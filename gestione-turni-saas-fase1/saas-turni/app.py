#!/usr/bin/env python3
"""
Backend multi-tenant per la piattaforma SaaS di gestione turni.

Modello dati (blocco B della roadmap):

    Azienda (organizzazione)  →  Locale (punto vendita)  →  AppState (i turni)
         └→ Utente (chi accede, con un ruolo e, se serve, legato a un solo locale)

Prima un'azienda era anche il login ed era anche il locale: una catena con otto
ristoranti erano otto account scollegati, e titolare e responsabili condividevano
le stesse credenziali. Ora:

  * un'organizzazione può avere più locali, ognuno con i propri turni;
  * più persone accedono con credenziali proprie e ruoli diversi;
  * un utente può essere legato a un solo locale oppure vederli tutti.

Ruoli:
  titolare      tutto, compresa la gestione di utenti e locali
  responsabile  turni, personale, assenze del proprio locale
  lettura       può solo guardare

L'isolamento resta la regola più importante di tutta l'architettura: il locale
non viene MAI preso da quello che manda il client, ma sempre dalla sessione
autenticata, e ogni accesso verifica che quel locale appartenga
all'organizzazione dell'utente.

Avvio locale (usa SQLite, nessuna configurazione):
    pip install -r requirements.txt
    python app.py
    flask --app app create-tenant

Deploy su Render: vedi README.md.
"""
import json
import os
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, jsonify, request, redirect, url_for, render_template,
    send_from_directory, abort, session,
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from migrazione import migra_a_locali, RUOLI, LIVELLO_RUOLO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PORT = int(os.environ.get("PORT", "5000"))

# --- configurazione database -------------------------------------------------
# In locale, se non è impostata nessuna DATABASE_URL, usa SQLite (zero setup).
# In produzione (Render) DATABASE_URL viene fornita automaticamente collegando
# un database Postgres al servizio. Render la espone come "postgres://...":
# SQLAlchemy 2.x vuole "postgresql://...", quindi la normalizziamo.
db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "turni.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app = Flask(__name__, static_folder=None, template_folder="templates")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-cambiami-in-produzione")

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = None


def now_utc():
    return datetime.now(timezone.utc)


# --- modelli -----------------------------------------------------------------

class Azienda(db.Model):
    """L'organizzazione cliente. Non è più un login: è il contenitore dei locali
    e degli utenti. L'email qui resta solo come riferimento amministrativo."""
    __tablename__ = "aziende"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), index=True)
    creato_il = db.Column(db.DateTime, default=now_utc)
    # marcatore della migrazione al modello a locali: impedisce che le vecchie
    # credenziali, ancora presenti su questa tabella, ricreino a ogni riavvio
    # utenti che nel frattempo sono stati cancellati apposta
    migrata_il = db.Column(db.String(40))


class Locale(db.Model):
    """Un punto vendita: ha i propri turni, il proprio personale, le proprie
    impostazioni. È l'unità su cui lavora tutta l'applicazione."""
    __tablename__ = "locali"

    id = db.Column(db.Integer, primary_key=True)
    azienda_id = db.Column(db.Integer, db.ForeignKey("aziende.id"), nullable=False, index=True)
    nome = db.Column(db.String(200), nullable=False)
    attivo = db.Column(db.Boolean, default=True)
    creato_il = db.Column(db.DateTime, default=now_utc)


class Utente(db.Model, UserMixin):
    """Chi accede. Appartiene a un'organizzazione, ha un ruolo, e può essere
    legato a un singolo locale (locale_id valorizzato) oppure vederli tutti
    (locale_id nullo, tipicamente il titolare)."""
    __tablename__ = "utenti"

    id = db.Column(db.Integer, primary_key=True)
    azienda_id = db.Column(db.Integer, db.ForeignKey("aziende.id"), nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    ruolo = db.Column(db.String(20), nullable=False, default="titolare")
    locale_id = db.Column(db.Integer, db.ForeignKey("locali.id"), nullable=True)
    creato_il = db.Column(db.DateTime, default=now_utc)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def livello(self):
        return LIVELLO_RUOLO.get(self.ruolo, 0)


class AppState(db.Model):
    """Il blob di stato di un LOCALE: impostazioni, personale, turni. Il numero
    di versione mantiene il meccanismo di concorrenza ottimistica già usato dal
    frontend. La vecchia colonna azienda_id resta popolata per i dati migrati,
    come rete di sicurezza: non viene più letta."""
    __tablename__ = "app_state"

    id = db.Column(db.Integer, primary_key=True)
    azienda_id = db.Column(db.Integer, db.ForeignKey("aziende.id"), nullable=True)
    locale_id = db.Column(db.Integer, db.ForeignKey("locali.id"), unique=True, nullable=True)
    version = db.Column(db.Integer, nullable=False, default=0)
    data = db.Column(db.Text)
    aggiornato_il = db.Column(db.DateTime, default=now_utc)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Utente, int(user_id))


# --- locale corrente e permessi ---------------------------------------------

def locali_visibili(utente):
    """I locali su cui l'utente può lavorare. Chi è legato a un locale vede
    solo quello; gli altri vedono tutti quelli della propria organizzazione."""
    q = Locale.query.filter_by(azienda_id=utente.azienda_id, attivo=True)
    if utente.locale_id:
        q = q.filter(Locale.id == utente.locale_id)
    return q.order_by(Locale.nome).all()


def locale_corrente():
    """Il locale su cui si sta lavorando. Non arriva mai dal client: o è quello
    fissato sull'utente, o è quello scelto in sessione e verificato, o il primo."""
    disponibili = locali_visibili(current_user)
    if not disponibili:
        return None
    if current_user.locale_id:
        return disponibili[0]
    scelto = session.get("locale_id")
    for loc in disponibili:
        if loc.id == scelto:
            return loc
    return disponibili[0]


def richiede_ruolo(minimo):
    """Decoratore: blocca chi non ha almeno il livello richiesto."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_user.livello < LIVELLO_RUOLO[minimo]:
                return jsonify({"error": "permesso negato", "serve": minimo}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


# --- pagine ------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        utente = Utente.query.filter_by(email=email).first()
        if utente and utente.check_password(password):
            login_user(utente, remember=True)
            session.pop("locale_id", None)
            return redirect(url_for("index"))
        error = "Email o password non corrette."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    session.pop("locale_id", None)
    logout_user()
    return redirect(url_for("login"))


# --- API ---------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_utc().isoformat()})


@app.route("/api/whoami")
@login_required
def whoami():
    loc = locale_corrente()
    azienda = db.session.get(Azienda, current_user.azienda_id)
    return jsonify({
        "utente": {"email": current_user.email, "ruolo": current_user.ruolo},
        "azienda": {"nome": azienda.nome if azienda else ""},
        "locale": {"id": loc.id, "nome": loc.nome} if loc else None,
        "locali": [{"id": l.id, "nome": l.nome} for l in locali_visibili(current_user)],
        "puoModificare": current_user.livello >= LIVELLO_RUOLO["responsabile"],
        "puoAmministrare": current_user.livello >= LIVELLO_RUOLO["titolare"],
    })


@app.route("/api/locale/<int:locale_id>", methods=["POST"])
@login_required
def cambia_locale(locale_id):
    """Cambia il locale su cui si lavora. Si può scegliere solo tra i propri:
    l'appartenenza viene verificata qui, non creduta sulla parola."""
    for loc in locali_visibili(current_user):
        if loc.id == locale_id:
            session["locale_id"] = locale_id
            return jsonify({"ok": True, "locale": {"id": loc.id, "nome": loc.nome}})
    return jsonify({"error": "locale non disponibile"}), 403


@app.route("/api/locali", methods=["POST"])
@login_required
@richiede_ruolo("titolare")
def crea_locale():
    payload = request.get_json(force=True, silent=True) or {}
    nome = (payload.get("nome") or "").strip()
    if not nome:
        return jsonify({"error": "nome mancante"}), 400
    loc = Locale(azienda_id=current_user.azienda_id, nome=nome, attivo=True)
    db.session.add(loc)
    db.session.commit()
    # il nuovo locale parte vuoto: il frontend creerà il suo stato al primo salvataggio
    return jsonify({"ok": True, "locale": {"id": loc.id, "nome": loc.nome}})


@app.route("/api/utenti", methods=["GET"])
@login_required
@richiede_ruolo("titolare")
def elenco_utenti():
    q = Utente.query.filter_by(azienda_id=current_user.azienda_id)
    if current_user.locale_id:
        # un titolare vincolato a un locale non deve vedere gli accessi degli altri locali
        q = q.filter(Utente.locale_id == current_user.locale_id)
    utenti = q.order_by(Utente.email).all()
    return jsonify({"utenti": [{
        "id": u.id, "email": u.email, "ruolo": u.ruolo,
        "locale_id": u.locale_id, "io": u.id == current_user.id,
    } for u in utenti]})


@app.route("/api/utenti", methods=["POST"])
@login_required
@richiede_ruolo("titolare")
def crea_utente():
    payload = request.get_json(force=True, silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    ruolo = payload.get("ruolo") or "responsabile"
    locale_id = payload.get("locale_id")
    if locale_id is not None:
        try:
            locale_id = int(locale_id)
        except (TypeError, ValueError):
            return jsonify({"error": "locale non valido"}), 400
    if not email or not password:
        return jsonify({"error": "email e password sono obbligatorie"}), 400
    if ruolo not in RUOLI:
        return jsonify({"error": "ruolo non valido"}), 400
    # chi è legato a un solo locale non può creare accessi che vedono gli altri:
    # senza questo controllo un titolare vincolato a un locale creerebbe un titolare
    # senza vincolo e da lì entrerebbe in tutti i locali dell'organizzazione
    if current_user.locale_id:
        if locale_id is None:
            locale_id = current_user.locale_id
        elif locale_id != current_user.locale_id:
            return jsonify({"error": "puoi creare accessi solo per il tuo locale"}), 403
    if locale_id is not None:
        if not any(l.id == locale_id for l in locali_visibili(current_user)):
            return jsonify({"error": "locale non disponibile"}), 403
    if Utente.query.filter_by(email=email).first():
        # messaggio volutamente generico: dire "esiste già" permetterebbe a un
        # cliente di scoprire gli indirizzi registrati da altri clienti
        return jsonify({"error": "questa email non è utilizzabile"}), 400
    u = Utente(azienda_id=current_user.azienda_id, email=email, ruolo=ruolo, locale_id=locale_id)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return jsonify({"ok": True, "id": u.id})


@app.route("/api/utenti/<int:utente_id>", methods=["DELETE"])
@login_required
@richiede_ruolo("titolare")
def elimina_utente(utente_id):
    if utente_id == current_user.id:
        return jsonify({"error": "non puoi eliminare il tuo stesso accesso"}), 400
    u = db.session.get(Utente, utente_id)
    if not u or u.azienda_id != current_user.azienda_id:
        return jsonify({"error": "utente non trovato"}), 404
    # chi è vincolato a un locale gestisce solo gli accessi di quel locale
    if current_user.locale_id and u.locale_id != current_user.locale_id:
        return jsonify({"error": "utente non trovato"}), 404
    titolari = Utente.query.filter_by(azienda_id=current_user.azienda_id, ruolo="titolare").count()
    if u.ruolo == "titolare" and titolari <= 1:
        return jsonify({"error": "deve restare almeno un titolare"}), 400
    db.session.delete(u)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/state", methods=["GET"])
@login_required
def get_state():
    loc = locale_corrente()
    if not loc:
        return jsonify({"version": 0, "data": None, "locale_id": None})
    row = AppState.query.filter_by(locale_id=loc.id).first()
    if not row:
        return jsonify({"version": 0, "data": None, "locale_id": loc.id})
    return jsonify({
        "version": row.version,
        "data": json.loads(row.data) if row.data else None,
        "locale_id": loc.id,
    })


@app.route("/api/state", methods=["PUT"])
@login_required
@richiede_ruolo("responsabile")
def put_state():
    loc = locale_corrente()
    if not loc:
        return jsonify({"error": "nessun locale associato a questo utente"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    client_version = payload.get("version", 0)
    data = payload.get("data")
    if data is None:
        return jsonify({"error": "dati mancanti"}), 400

    # Il locale corrente vive nella sessione, che è condivisa da tutte le schede
    # dello stesso browser: se in un'altra scheda si cambia locale, questa scheda
    # scriverebbe i propri turni sopra quelli di un'altra attività. Il client dice
    # su quale locale crede di stare lavorando, e qui si verifica che coincida.
    locale_atteso = payload.get("locale_id")
    if locale_atteso is not None and int(locale_atteso) != loc.id:
        return jsonify({
            "error": "locale-cambiato",
            "locale_id": loc.id,
            "messaggio": "Il locale selezionato è cambiato in un'altra scheda.",
        }), 409

    # with_for_update blocca la riga finché la transazione non finisce, per
    # evitare corse tra richieste concorrenti sullo stesso locale (su SQLite,
    # usato solo in locale, viene ignorato senza errore).
    row = db.session.execute(
        select(AppState).where(AppState.locale_id == loc.id).with_for_update()
    ).scalar_one_or_none()

    if not row:
        if client_version not in (0, None):
            db.session.rollback()
            return jsonify({"error": "conflitto", "version": 0, "data": None}), 409
        row = AppState(locale_id=loc.id, azienda_id=loc.azienda_id, version=1,
                       data=json.dumps(data), aggiornato_il=now_utc())
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            # due primi salvataggi simultanei sullo stesso locale nuovo: il lock di
            # riga non protegge una riga che ancora non esiste, ci pensa l'indice
            # univoco. Chi arriva secondo riceve un conflitto normale e fa merge,
            # invece di un errore 500 che gli farebbe perdere il lavoro.
            db.session.rollback()
            esistente = AppState.query.filter_by(locale_id=loc.id).first()
            return jsonify({
                "error": "conflitto",
                "version": esistente.version if esistente else 0,
                "data": json.loads(esistente.data) if (esistente and esistente.data) else None,
            }), 409
        return jsonify({"version": 1})

    if client_version != row.version:
        current_data = json.loads(row.data) if row.data else None
        db.session.rollback()
        return jsonify({"error": "conflitto", "version": row.version, "data": current_data}), 409

    row.version += 1
    row.data = json.dumps(data)
    row.aggiornato_il = now_utc()
    db.session.commit()
    return jsonify({"version": row.version})


# --- onboarding dal browser --------------------------------------------------
# Protetta da un token segreto nell'URL, letto da SETUP_TOKEN: senza il token
# giusto la pagina restituisce 404, come se non esistesse.
@app.route("/setup/<token>", methods=["GET", "POST"])
def setup_tenant(token):
    expected = os.environ.get("SETUP_TOKEN")
    if not expected or token != expected:
        abort(404)

    error = None
    ok_message = None
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not (nome and email and password):
            error = "Compila tutti i campi."
        elif Utente.query.filter_by(email=email).first():
            error = "Esiste già un utente con questa email."
        else:
            azienda = Azienda(nome=nome, email=email)
            db.session.add(azienda)
            db.session.flush()
            locale = Locale(azienda_id=azienda.id, nome=nome, attivo=True)
            db.session.add(locale)
            utente = Utente(azienda_id=azienda.id, email=email, ruolo="titolare", locale_id=None)
            utente.set_password(password)
            db.session.add(utente)
            db.session.commit()
            ok_message = f"Account creato per '{nome}' ({email}), con il primo locale. Ora può accedere da /login."
    return render_template("setup.html", error=error, ok_message=ok_message)


# --- pannello amministratore -------------------------------------------------

def _admin_check(token):
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or token != expected:
        abort(404)


@app.route("/admin/<token>")
def admin_panel(token):
    _admin_check(token)
    aziende = Azienda.query.order_by(Azienda.creato_il.desc()).all()
    righe = []
    for az in aziende:
        locali = Locale.query.filter_by(azienda_id=az.id).order_by(Locale.nome).all()
        stati = {s.locale_id: s for s in AppState.query.filter(
            AppState.locale_id.in_([l.id for l in locali] or [-1])).all()}
        righe.append({
            "azienda": az,
            "locali": [{
                "locale": l,
                "ha_dati": l.id in stati,
                "versione": stati[l.id].version if l.id in stati else 0,
                "aggiornato_il": stati[l.id].aggiornato_il if l.id in stati else None,
            } for l in locali],
            "utenti": Utente.query.filter_by(azienda_id=az.id).order_by(Utente.email).all(),
            # compatibilità con il template attuale, che mostra una riga per azienda
            "ha_dati": any(l.id in stati for l in locali),
            "versione": max([stati[l.id].version for l in locali if l.id in stati] or [0]),
            # aggiornato_il è nullable: senza il filtro, max() confronterebbe
            # datetime e None e romperebbe il pannello per tutti i clienti
            "aggiornato_il": max(
                [stati[l.id].aggiornato_il for l in locali
                 if l.id in stati and stati[l.id].aggiornato_il is not None] or [None]
            ),
        })
    return render_template("admin.html", token=token, righe=righe)


@app.route("/admin/<token>/accedi-come/<int:azienda_id>")
def admin_impersonate(token, azienda_id):
    _admin_check(token)
    utente = Utente.query.filter_by(azienda_id=azienda_id, ruolo="titolare").order_by(Utente.id).first()
    if not utente:
        utente = Utente.query.filter_by(azienda_id=azienda_id).order_by(Utente.id).first()
    if not utente:
        abort(404)
    login_user(utente, remember=False)
    session.pop("locale_id", None)
    return redirect(url_for("index"))


@app.route("/admin/<token>/elimina/<int:azienda_id>", methods=["POST"])
def admin_delete(token, azienda_id):
    _admin_check(token)
    azienda = db.session.get(Azienda, azienda_id)
    if azienda:
        # l'ordine conta: utenti.locale_id e app_state.locale_id puntano a locali,
        # quindi i locali si cancellano per ultimi, altrimenti PostgreSQL rifiuta
        # la cancellazione per violazione di chiave esterna
        locali_ids = [l.id for l in Locale.query.filter_by(azienda_id=azienda.id).all()]
        Utente.query.filter_by(azienda_id=azienda.id).delete(synchronize_session=False)
        if locali_ids:
            AppState.query.filter(AppState.locale_id.in_(locali_ids)).delete(synchronize_session=False)
        AppState.query.filter_by(azienda_id=azienda.id).delete(synchronize_session=False)
        Locale.query.filter_by(azienda_id=azienda.id).delete(synchronize_session=False)
        db.session.delete(azienda)
        db.session.commit()
    return redirect(url_for("admin_panel", token=token))


# --- gestione da riga di comando ---------------------------------------------

@app.cli.command("create-tenant")
def create_tenant():
    """Crea una nuova organizzazione con il suo primo locale e il titolare:
    flask --app app create-tenant"""
    import getpass
    db.create_all()
    nome = input("Nome attività: ").strip()
    email = input("Email di accesso: ").strip().lower()
    password = getpass.getpass("Password: ")
    if Utente.query.filter_by(email=email).first():
        print("Esiste già un utente con questa email.")
        return
    azienda = Azienda(nome=nome, email=email)
    db.session.add(azienda)
    db.session.flush()
    locale = Locale(azienda_id=azienda.id, nome=nome, attivo=True)
    db.session.add(locale)
    utente = Utente(azienda_id=azienda.id, email=email, ruolo="titolare", locale_id=None)
    utente.set_password(password)
    db.session.add(utente)
    db.session.commit()
    print(f"Creata organizzazione '{nome}' (id={azienda.id}) con locale '{nome}' e titolare {email}.")


@app.cli.command("aggiungi-locale")
def aggiungi_locale():
    """Aggiunge un locale a un'organizzazione esistente:
    flask --app app aggiungi-locale"""
    db.create_all()
    for az in Azienda.query.order_by(Azienda.id).all():
        print(f"  {az.id}: {az.nome}")
    azienda_id = int(input("Id organizzazione: ").strip())
    nome = input("Nome del nuovo locale: ").strip()
    if not db.session.get(Azienda, azienda_id):
        print("Organizzazione inesistente.")
        return
    loc = Locale(azienda_id=azienda_id, nome=nome, attivo=True)
    db.session.add(loc)
    db.session.commit()
    print(f"Creato locale '{nome}' (id={loc.id}).")


# --- avvio -------------------------------------------------------------------

def _esegui_sql(sql, params=None):
    db.session.execute(text(sql), params or {})


def _leggi_sql(sql, params=None):
    return [tuple(r) for r in db.session.execute(text(sql), params or {}).all()]


with app.app_context():
    db.create_all()
    # porta i dati dei clienti già attivi al nuovo modello (idempotente)
    try:
        dialect = db.engine.dialect.name
        esito = migra_a_locali(_esegui_sql, _leggi_sql, dialect)
        db.session.commit()
        if any(v for v in esito.values()):
            print("Migrazione schema →  locali/utenti:", esito)
    except Exception as exc:
        # Meglio non partire che partire a metà: con la colonna locale_id mancante
        # l'app risponderebbe 500 a ogni richiesta mentre l'health check resta verde,
        # cioè un guasto totale e silenzioso. Se l'avvio fallisce, il deploy si ferma
        # e la versione precedente resta in servizio.
        db.session.rollback()
        print("MIGRAZIONE SCHEMA NON RIUSCITA — avvio interrotto:", exc)
        raise


if __name__ == "__main__":
    print("Piattaforma turni — server multi-tenant")
    print(f"Apri http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=os.environ.get("FLASK_DEBUG") == "1")
