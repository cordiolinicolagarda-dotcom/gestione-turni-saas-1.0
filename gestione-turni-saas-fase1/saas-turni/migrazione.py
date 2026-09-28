"""
Migrazione dello schema dati: da "un'azienda = un login = un locale" a
"organizzazione → locali → utenti con ruoli".

Sta in un modulo separato, e lavora su due sole funzioni passate dall'esterno
(esegui / leggi), per un motivo preciso: è il pezzo più delicato di tutto il
blocco B — tocca i dati di clienti già in produzione — e in questo modo si può
provare davvero su un database SQLite vero, senza tirarsi dietro Flask.

I segnaposto sono in stile ":nome", che vanno bene sia per SQLAlchemy text()
sia per il modulo sqlite3 della libreria standard: la stessa identica funzione
che gira in produzione è quella che passa dai test.

La migrazione è additiva: non cancella e non riscrive nulla di esistente.
La vecchia colonna app_state.azienda_id resta al suo posto come rete di
sicurezza; viene solo affiancata da locale_id.

Prima:                          Dopo:
  aziende (id, nome, email,       aziende  (id, nome, email, ...)      organizzazione
           password_hash)         locali   (id, azienda_id, nome)      il singolo punto vendita
  app_state (azienda_id, ...)     utenti   (id, azienda_id, email,     chi accede, con che ruolo
                                            password_hash, ruolo,
                                            locale_id)
                                  app_state (locale_id, azienda_id, ...)
"""

RUOLI = ("titolare", "responsabile", "lettura")
LIVELLO_RUOLO = {"lettura": 1, "responsabile": 2, "titolare": 3}


def colonne_di(leggi, tabella, dialect):
    """Elenco delle colonne di una tabella, in SQLite e in PostgreSQL."""
    if dialect == "postgresql":
        righe = leggi(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = current_schema()",
            {"t": tabella},
        )
        return {r[0] for r in righe}
    # PRAGMA non accetta segnaposto: il nome tabella qui è sempre una costante interna
    righe = leggi("PRAGMA table_info(%s)" % tabella)
    return {r[1] for r in righe}


def tabella_esiste(leggi, tabella, dialect):
    if dialect == "postgresql":
        righe = leggi(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = :t AND table_schema = current_schema()",
            {"t": tabella},
        )
    else:
        righe = leggi(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = :t",
            {"t": tabella},
        )
    return len(righe) > 0


# Le colonne che il modello ATTUALE dichiara, tabella per tabella. Tutto il resto,
# in quelle tabelle, è un residuo del modello vecchio (credenziali sull'azienda,
# soprattutto). I dati residui si tengono — sono la rete di sicurezza della
# migrazione — ma il vincolo NOT NULL va tolto: il codice non riempie più quelle
# colonne, quindi ogni inserimento nuovo morirebbe su di esse. È esattamente quello
# che è successo in produzione al primo invito: "null value in column password_hash
# of relation aziende violates not-null constraint".
COLONNE_MODELLO = {
    "aziende": {"id", "nome", "email", "creato_il", "migrata_il"},
    "locali": {"id", "azienda_id", "nome", "attivo", "creato_il"},
    "utenti": {"id", "azienda_id", "email", "password_hash", "ruolo", "locale_id",
               "creato_il", "token_attivazione", "invitato_il", "attivato_il", "sessione"},
    "app_state": {"id", "azienda_id", "locale_id", "version", "data", "aggiornato_il"},
}


def allenta_colonne_residue(esegui, leggi, dialect):
    """Toglie il NOT NULL dalle colonne che il modello attuale non usa più.

    Solo PostgreSQL: in SQLite le tabelle le crea create_all() da zero, quindi
    colonne residue non esistono, e per togliere un NOT NULL bisognerebbe comunque
    ricostruire la tabella.

    Idempotente: in PostgreSQL DROP NOT NULL su una colonna già nullable non è un
    errore, e la lista bianca protegge le colonne che devono restare obbligatorie —
    una chiave primaria o "nome" non vengono mai toccate.
    """
    if dialect != "postgresql":
        return 0
    quante = 0
    for tabella in sorted(COLONNE_MODELLO):
        if not tabella_esiste(leggi, tabella, dialect):
            continue
        attese = COLONNE_MODELLO[tabella]
        righe = leggi(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = current_schema() "
            "AND is_nullable = 'NO'",
            {"t": tabella},
        )
        for r in righe:
            nome = r[0]
            if nome in attese:
                continue
            esegui('ALTER TABLE %s ALTER COLUMN "%s" DROP NOT NULL' % (tabella, nome))
            quante += 1
    return quante


def migra_a_locali(esegui, leggi, dialect="sqlite"):
    """Porta uno schema vecchio al nuovo modello. Idempotente: eseguirla due
    volte non cambia niente e non duplica nulla.

    esegui(sql, params=None) -> None
    leggi(sql, params=None)  -> lista di tuple

    Ritorna un riepilogo di cosa è stato fatto, utile nei log di avvio.
    """
    fatto = {
        "locali_creati": 0,
        "utenti_creati": 0,
        "stati_collegati": 0,
        "colonna_aggiunta": False,
        "da_verificare": [],   # casi ambigui: NON si tira a indovinare, si segnala
        "stati_orfani": 0,     # stati che puntano a un'azienda che non esiste più
        "colonne_invito": 0,   # colonne aggiunte per gli inviti
        "vincoli_allentati": 0,  # NOT NULL tolti da colonne del modello vecchio
    }

    # database appena creato: create_all() ha già fatto tutto, non c'è nulla da migrare
    if not tabella_esiste(leggi, "aziende", dialect):
        return fatto
    if not tabella_esiste(leggi, "locali", dialect) or not tabella_esiste(leggi, "utenti", dialect):
        return fatto

    ha_stato = tabella_esiste(leggi, "app_state", dialect)

    # 1) app_state.locale_id: aggiunta additiva, senza toccare azienda_id
    if ha_stato:
        colonne_stato = colonne_di(leggi, "app_state", dialect)
        if colonne_stato and "locale_id" not in colonne_stato:
            esegui("ALTER TABLE app_state ADD COLUMN locale_id INTEGER")
            fatto["colonna_aggiunta"] = True
        # il vincolo di unicità dichiarato sul modello non arriva alle tabelle già
        # esistenti (create_all non altera nulla): senza indice, due salvataggi
        # simultanei sul primo stato di un locale creerebbero due righe gemelle
        # e da lì in poi quel locale non sarebbe più salvabile.
        # Prima si verifica che non ci siano già duplicati, altrimenti la creazione
        # dell'indice fallirebbe e bloccherebbe l'avvio.
        duplicati = leggi(
            "SELECT locale_id FROM app_state WHERE locale_id IS NOT NULL "
            "GROUP BY locale_id HAVING COUNT(*) > 1"
        )
        if duplicati:
            fatto["da_verificare"].append({
                "problema": "stati duplicati sullo stesso locale",
                "locali": [d[0] for d in duplicati],
            })
        else:
            esegui("CREATE UNIQUE INDEX IF NOT EXISTS ix_app_state_locale ON app_state (locale_id)")

    # 1-bis) colonne dell'invito su "utenti": additive, come tutto il resto.
    #        Servono a creare un accesso che esiste ma non si può ancora usare,
    #        finché la persona non apre il link e sceglie la sua password.
    colonne_utenti = colonne_di(leggi, "utenti", dialect)
    for nome_col, tipo_col in (("token_attivazione", "VARCHAR(64)"),
                               ("invitato_il", "VARCHAR(40)"),
                               ("attivato_il", "VARCHAR(40)"),
                               # NOT NULL con un default: le righe già presenti
                               # partono da 1, come i nuovi utenti
                               ("sessione", "INTEGER NOT NULL DEFAULT 1")):
        if colonne_utenti and nome_col not in colonne_utenti:
            esegui("ALTER TABLE utenti ADD COLUMN %s %s" % (nome_col, tipo_col))
            fatto["colonne_invito"] = fatto.get("colonne_invito", 0) + 1
    if colonne_utenti and "token_attivazione" in colonne_utenti or fatto.get("colonne_invito"):
        esegui("CREATE INDEX IF NOT EXISTS ix_utenti_token_attivazione ON utenti (token_attivazione)")

    # Va fatto SEMPRE, anche su un database già migrato: il marcatore migrata_il
    # dice che gli utenti sono stati creati, non che i vincoli vecchi sono spariti.
    fatto["vincoli_allentati"] = allenta_colonne_residue(esegui, leggi, dialect)

    colonne_azienda = colonne_di(leggi, "aziende", dialect)
    ha_credenziali_azienda = "password_hash" in colonne_azienda and "email" in colonne_azienda

    # 2) marcatore di avvenuta migrazione: senza, a ogni riavvio le credenziali
    #    ancora presenti su "aziende" ricreerebbero utenti già cancellati a mano
    #    — un accesso revocato tornerebbe in vita al primo deploy
    if "migrata_il" not in colonne_azienda:
        esegui("ALTER TABLE aziende ADD COLUMN migrata_il VARCHAR(40)")
        colonne_azienda.add("migrata_il")

    for azienda_id, nome, migrata_il in leggi("SELECT id, nome, migrata_il FROM aziende"):
        gia_migrata = bool(migrata_il)

        # 3) ogni azienda senza locale ne riceve uno, con lo stesso nome
        esistenti = leggi(
            "SELECT id FROM locali WHERE azienda_id = :a ORDER BY id", {"a": azienda_id}
        )
        if esistenti:
            locale_id = esistenti[0][0]
        else:
            # "attivo" come parametro e non come letterale 1: su PostgreSQL la colonna
            # è BOOLEAN e non accetta un intero, su SQLite il valore arriva comunque giusto
            esegui(
                "INSERT INTO locali (azienda_id, nome, attivo) VALUES (:a, :n, :att)",
                {"a": azienda_id, "n": nome, "att": True},
            )
            locale_id = leggi(
                "SELECT id FROM locali WHERE azienda_id = :a ORDER BY id DESC", {"a": azienda_id}
            )[0][0]
            fatto["locali_creati"] += 1

        # 4) le credenziali dell'azienda diventano l'utente titolare, con la stessa
        #    password: chi già accede continua ad accedere esattamente come prima.
        #    Una sola volta nella vita del database, mai più.
        if ha_credenziali_azienda and not gia_migrata:
            righe = leggi(
                "SELECT email, password_hash FROM aziende WHERE id = :a", {"a": azienda_id}
            )
            if righe and righe[0][0] and righe[0][1]:
                email, password_hash = righe[0][0], righe[0][1]
                gia_presente = leggi("SELECT id FROM utenti WHERE email = :e", {"e": email})
                if not gia_presente:
                    esegui(
                        "INSERT INTO utenti (azienda_id, email, password_hash, ruolo, locale_id) "
                        "VALUES (:a, :e, :p, 'titolare', NULL)",
                        {"a": azienda_id, "e": email, "p": password_hash},
                    )
                    fatto["utenti_creati"] += 1

        # 5) lo stato dell'azienda diventa lo stato del suo locale. Si collega solo
        #    quando la corrispondenza è certa: un solo locale e un solo stato. Se
        #    l'azienda ha già più locali non si indovina a quale appartenga lo stato,
        #    si segnala e basta.
        if ha_stato:
            da_collegare = leggi(
                "SELECT id FROM app_state WHERE azienda_id = :a AND locale_id IS NULL",
                {"a": azienda_id},
            )
            if da_collegare and (len(esistenti) > 1 or len(da_collegare) > 1):
                fatto["da_verificare"].append({
                    "azienda_id": azienda_id, "nome": nome,
                    "locali": len(esistenti) or 1, "stati_da_collegare": len(da_collegare),
                })
            else:
                for riga in da_collegare:
                    esegui(
                        "UPDATE app_state SET locale_id = :l WHERE id = :s",
                        {"l": locale_id, "s": riga[0]},
                    )
                    fatto["stati_collegati"] += 1

        if not gia_migrata:
            esegui(
                "UPDATE aziende SET migrata_il = :q WHERE id = :a",
                {"q": _adesso(), "a": azienda_id},
            )

    # 6) stati che puntano a un'azienda cancellata: non si perdono, ma non sono
    #    raggiungibili da nessuna rotta. Vanno almeno contati, per non scoprirlo mai.
    if ha_stato:
        orfani = leggi(
            "SELECT COUNT(*) FROM app_state WHERE locale_id IS NULL AND "
            "(azienda_id IS NULL OR azienda_id NOT IN (SELECT id FROM aziende))"
        )
        fatto["stati_orfani"] = orfani[0][0] if orfani else 0

    return fatto


def _adesso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
