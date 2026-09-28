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
import secrets
from datetime import datetime, timedelta, timezone
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
# un database Postgres al servizio.

def driver_postgres_disponibile():
    """Quale driver PostgreSQL è davvero installato in questo ambiente.

    psycopg2 per primo perché è quello che mettiamo in requirements.txt; psycopg
    (la versione 3) come alternativa, per chi installa quello.
    """
    import importlib.util
    for nome in ("psycopg2", "psycopg"):
        if importlib.util.find_spec(nome) is not None:
            return nome
    return None


def normalizza_url_database(url):
    """Rende esplicito lo schema e il driver dell'URL del database.

    Due trappole, entrambe già costate un deploy fallito:

    1. Render espone la connessione come "postgres://...", che SQLAlchemy non
       riconosce più: va scritto "postgresql://...".

    2. Da SQLAlchemy 2.1 "postgresql://" NON significa più psycopg2: il driver
       predefinito è diventato psycopg (la versione 3). Con psycopg2 installato
       e psycopg no, l'avvio muore con
       "ModuleNotFoundError: No module named 'psycopg'" — un errore che parla di
       un pacchetto che non abbiamo mai chiesto. Quindi il driver lo scegliamo
       qui, fra quelli presenti davvero, invece di lasciarlo decidere alla
       versione di SQLAlchemy che il giorno del deploy si trova installata.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # se il driver è già scritto a mano (postgresql+psycopg2://...) non si tocca
    if url.startswith("postgresql://"):
        driver = driver_postgres_disponibile()
        if driver:
            url = "postgresql+" + driver + "://" + url[len("postgresql://"):]
    return url


db_url = normalizza_url_database(
    os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "turni.db"))
)

app = Flask(__name__, static_folder=None, template_folder="templates")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

# In produzione siamo dietro il proxy di Render, che parla https con il browser e
# http con noi. Senza questo, url_for(_external=True) genera link "http://" e i
# cookie non possono essere marcati Secure.
IN_PRODUZIONE = bool(os.environ.get("RENDER") or db_url.startswith("postgresql"))
if IN_PRODUZIONE:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

# La chiave firma i cookie di sessione: se resta quella scritta nel repository,
# chiunque legga il codice può forgiare un cookie ed entrare come qualsiasi
# titolare. In produzione non la usiamo mai: si genera una chiave casuale e si
# grida nei log. Il prezzo è che a ogni riavvio tutti devono rientrare — meglio
# di un portone aperto, e si risolve impostando SECRET_KEY su Render.
_chiave = os.environ.get("SECRET_KEY")
SECRET_KEY_MANCANTE = not _chiave
if not _chiave:
    _chiave = "dev-secret-cambiami-in-produzione" if not IN_PRODUZIONE else secrets.token_urlsafe(48)
app.secret_key = _chiave

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
    # Invito: finché token_attivazione è valorizzato l'account esiste ma non si può
    # usare. La password_hash resta NOT NULL (contiene un valore casuale che nessuno
    # conosce) perché cambiare la nullabilità di una colonna su un database già in
    # produzione è il tipo di migrazione che può lasciare l'app a metà.
    token_attivazione = db.Column(db.String(64), index=True)
    invitato_il = db.Column(db.String(40))
    attivato_il = db.Column(db.String(40))
    # Contatore delle sessioni valide. Finisce dentro l'identificativo firmato nel
    # cookie: incrementarlo fa scadere di colpo tutti i cookie già emessi per questo
    # utente. Senza, un "ricordami" rubato resterebbe valido per un anno anche dopo
    # aver cambiato la password, e reimpostare la password non servirebbe a niente.
    sessione = db.Column(db.Integer, nullable=False, default=1)

    @property
    def in_attesa(self):
        return bool(self.token_attivazione)

    def get_id(self):
        return "%d|%d" % (self.id, self.sessione or 1)

    def invalida_sessioni(self):
        self.sessione = (self.sessione or 1) + 1

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        # cambiare password significa "da adesso vale solo questa": i cookie
        # emessi prima non devono più aprire niente
        self.invalida_sessioni()

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
    """Legge l'identificativo "<id>|<sessione>" scritto nel cookie. Un cookie con un
    contatore diverso da quello sul database è vecchio e non vale più. I cookie del
    formato precedente (solo l'id) vengono rifiutati: chi ce li ha rientra una volta."""
    testo = str(user_id or "")
    if "|" not in testo:
        return None
    pezzi = testo.split("|", 1)
    try:
        uid, sess = int(pezzi[0]), int(pezzi[1])
    except ValueError:
        return None
    utente = db.session.get(Utente, uid)
    if not utente or (utente.sessione or 1) != sess:
        return None
    # un account invitato e non ancora attivato non deve poter navigare: l'unica
    # rotta che lo logga volutamente è /attiva, che il token lo azzera prima
    if utente.in_attesa:
        return None
    return utente


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
        if utente and utente.in_attesa and utente.check_password(password):
            # solo a chi indovina la password diciamo che l'account esiste ma non è
            # attivato: dirlo a chiunque trasformerebbe questa pagina in un elenco di
            # account con l'invito mai aperto, cioè i più facili da prendere
            error = "Questo accesso non è ancora stato attivato: apri il link di attivazione che ti è stato inviato."
        elif utente and not utente.in_attesa and utente.check_password(password):
            login_user(utente, remember=True)
            session.pop("locale_id", None)
            return redirect(url_for("index"))
        else:
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
    # si contano solo i titolari che possono davvero entrare: un titolare con
    # l'invito ancora da aprire non è una rete di sicurezza, e se l'invito scade
    # l'organizzazione resta chiusa fuori
    titolari = Utente.query.filter_by(azienda_id=current_user.azienda_id, ruolo="titolare").filter(
        Utente.token_attivazione.is_(None)).count()
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
    if not expected or not secrets.compare_digest(str(token), str(expected)):
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
    return render_template(
        "admin.html", token=token, righe=righe,
        # il link contiene un token: in querystring finirebbe nei log delle richieste
        # di Render, sulla stessa riga del token del pannello. Passa dalla sessione e
        # si consuma alla prima lettura.
        invito=session.pop("ultimo_invito", None),
        esito=session.pop("ultimo_esito", None),
        errore=session.pop("ultimo_errore", None),
        smtp_attivo=bool(os.environ.get("SMTP_HOST")),
        giorni_invito=GIORNI_VALIDITA_INVITO,
    )


@app.route("/admin/<token>/accedi-come/<int:azienda_id>", methods=["POST"])
def admin_impersonate(token, azienda_id):
    """Entra come il cliente. Deve essere POST: come link, un prefetch del browser o
    uno scanner aprirebbe la sessione senza che nessuno abbia cliccato."""
    _admin_check(token)
    # solo account già attivati: quelli in attesa non superano load_user, e l'admin
    # si troverebbe rispedito al login senza capire perché
    attivi = Utente.query.filter_by(azienda_id=azienda_id).filter(
        Utente.token_attivazione.is_(None)).order_by(Utente.id)
    utente = attivi.filter_by(ruolo="titolare").first() or attivi.first()
    if not utente:
        session["ultimo_errore"] = ("Questa attività non ha ancora nessun accesso attivato: "
                                    "finché la persona non apre il link di attivazione e sceglie la password, "
                                    "non c'è un utente con cui entrare.")
        return redirect(url_for("admin_panel", token=token))
    login_user(utente, remember=False)
    session.pop("locale_id", None)
    return redirect(url_for("index"))


GIORNI_VALIDITA_INVITO = 14


def _nuovo_token_invito():
    return secrets.token_urlsafe(32)


def _invito_scaduto(utente):
    """In dubbio si risponde "scaduto": un controllo di scadenza che sbaglia in
    apertura lascia un token valido per sempre. Rifare il link costa un clic."""
    if not utente.invitato_il:
        return True
    try:
        quando = datetime.fromisoformat(utente.invitato_il)
    except ValueError:
        return True
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return now_utc() - quando > timedelta(days=GIORNI_VALIDITA_INVITO)


def _link_attivazione(utente):
    return url_for("attiva_account", token=utente.token_attivazione, _external=True)


def _invia_invito(destinatario, link, nome_attivita):
    """Manda l'email dell'invito, se e solo se l'SMTP è configurato.

    Il link viene comunque sempre mostrato nel pannello: se la posta non parte
    (server non configurato, credenziali sbagliate, destinatario che rifiuta)
    l'invito non deve andare perso. Ritorna (inviata, motivo).
    """
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False, "SMTP non configurato: copia il link e mandalo tu."
    try:
        import smtplib
        from email.message import EmailMessage
        porta = int(os.environ.get("SMTP_PORT", "587"))
        mittente = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "no-reply@localhost"
        msg = EmailMessage()
        msg["Subject"] = "Attiva il tuo accesso a Gestione turni"
        msg["From"] = mittente
        msg["To"] = destinatario
        msg.set_content(
            "Ciao,\n\n"
            f"è stato creato per te l'accesso a Gestione turni ({nome_attivita}).\n"
            "Apri questo link per scegliere la tua password e cominciare:\n\n"
            f"{link}\n\n"
            f"Il link resta valido {GIORNI_VALIDITA_INVITO} giorni.\n"
        )
        with smtplib.SMTP(host, porta, timeout=15) as smtp:
            smtp.starttls()
            utente_smtp = os.environ.get("SMTP_USER")
            if utente_smtp:
                smtp.login(utente_smtp, os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True, None
    except Exception as exc:   # la posta non deve mai far fallire l'invito
        return False, f"Invio email non riuscito ({exc}). Copia il link e mandalo tu."


@app.route("/admin/<token>/invita", methods=["POST"])
def admin_invita(token):
    """Crea un nuovo portale (organizzazione + primo locale + titolare) e
    restituisce il link di attivazione da mandare alla persona."""
    _admin_check(token)
    email = (request.form.get("email") or "").strip().lower()
    nome = (request.form.get("nome") or "").strip()
    if not email or "@" not in email:
        session["ultimo_errore"] = "Indirizzo email non valido."
        return redirect(url_for("admin_panel", token=token))
    if not nome:
        nome = email.split("@")[0]
    esistente = Utente.query.filter_by(email=email).first()
    if esistente:
        session["ultimo_errore"] = (
            "Questa email è già usata da un accesso esistente"
            + (" (attività: " + (db.session.get(Azienda, esistente.azienda_id).nome
                                 if db.session.get(Azienda, esistente.azienda_id) else "—") + ")")
            + ". Ogni indirizzo può appartenere a un solo accesso: usa un'altra email, "
              "oppure — se è la stessa persona che deve rientrare — premi “rifai il link” "
              "sulla riga della sua attività qui sotto.")
        return redirect(url_for("admin_panel", token=token))

    azienda = Azienda(nome=nome, email=email)
    db.session.add(azienda)
    db.session.flush()
    db.session.add(Locale(azienda_id=azienda.id, nome=nome, attivo=True))
    utente = Utente(azienda_id=azienda.id, email=email, ruolo="titolare", locale_id=None)
    # password casuale che nessuno conosce: serve solo a tenere la colonna piena
    # finché la persona non sceglie la sua durante l'attivazione
    utente.set_password(secrets.token_urlsafe(32))
    utente.token_attivazione = _nuovo_token_invito()
    utente.invitato_il = now_utc().isoformat()
    db.session.add(utente)
    try:
        db.session.commit()
    except Exception:
        # due creazioni contemporanee sulla stessa email: meglio il messaggio già
        # scritto sopra che un 500 nudo, e niente mezza azienda in giro
        db.session.rollback()
        session["ultimo_errore"] = ("Non è stato possibile creare questo accesso: probabilmente "
                                    "l'email è già stata usata in questo momento. Riprova.")
        return redirect(url_for("admin_panel", token=token))

    link = _link_attivazione(utente)
    inviata, motivo = _invia_invito(email, link, nome)
    session["ultimo_invito"] = link
    session["ultimo_esito"] = ("Email inviata a " + email) if inviata else motivo
    return redirect(url_for("admin_panel", token=token))


@app.route("/admin/<token>/rigenera-invito/<int:utente_id>", methods=["POST"])
def admin_rigenera_invito(token, utente_id):
    """Rifà il link: per un invito scaduto o perso, oppure come reimpostazione della
    password di chi ha già attivato e non riesce più a entrare. Sono due cose diverse e
    il modulo deve dire quale intende, perché la seconda **revoca la password attuale**:
    farlo per sbaglio su un account funzionante chiude fuori il cliente."""
    _admin_check(token)
    utente = db.session.get(Utente, utente_id)
    if not utente:
        abort(404)
    reimposta = (request.form.get("reimposta") == "1")
    if not utente.in_attesa and not reimposta:
        # la persona ha attivato mentre il pannello era aperto: non le si tolgono
        # le chiavi di casa per una pagina non aggiornata
        session["ultimo_errore"] = (
            utente.email + " ha già attivato il suo accesso, quindi non serve nessun link. "
            "Se non riesce a entrare, usa “reimposta la password”: quello sì rifà il link, "
            "ma la password attuale smette di funzionare.")
        return redirect(url_for("admin_panel", token=token))

    utente.token_attivazione = _nuovo_token_invito()
    utente.invitato_il = now_utc().isoformat()
    utente.attivato_il = None
    # i cookie "ricordami" già in circolazione per questo account non devono
    # sopravvivere a una reimpostazione della password
    utente.invalida_sessioni()
    db.session.commit()
    link = _link_attivazione(utente)
    inviata, motivo = _invia_invito(utente.email, link, utente.email)
    session["ultimo_invito"] = link
    session["ultimo_esito"] = ("Email inviata a " + utente.email) if inviata else motivo
    return redirect(url_for("admin_panel", token=token))


@app.route("/attiva/<token>", methods=["GET", "POST"])
def attiva_account(token):
    """La persona sceglie la sua password e entra per la prima volta."""
    utente = Utente.query.filter_by(token_attivazione=token).first()
    if not utente:
        return render_template("attiva.html", stato="non_valido"), 404
    if _invito_scaduto(utente):
        return render_template("attiva.html", stato="scaduto", email=utente.email), 410

    errore = None
    if request.method == "POST":
        pw1 = request.form.get("password") or ""
        pw2 = request.form.get("password2") or ""
        if len(pw1) < 8:
            errore = "La password deve avere almeno 8 caratteri."
        elif pw1 != pw2:
            errore = "Le due password non coincidono."
        else:
            utente.set_password(pw1)
            utente.token_attivazione = None
            utente.attivato_il = now_utc().isoformat()
            db.session.commit()
            login_user(utente, remember=True)
            session.pop("locale_id", None)
            # il frontend legge questo parametro e apre la procedura guidata
            return redirect(url_for("index") + "?benvenuto=1")
    return render_template("attiva.html", stato="ok", email=utente.email, errore=errore)


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
    # una riga sola nei log, ma è la prima cosa da guardare quando l'avvio fallisce:
    # dice con quale motore e con quale driver ci si sta collegando davvero
    print("Database: %s (driver: %s)" % (db.engine.dialect.name, db.engine.dialect.driver))
    print("Configurazione: SECRET_KEY=%s ADMIN_TOKEN=%s SMTP_HOST=%s produzione=%s" % (
        "assente (generata a caso: tutti dovranno rientrare a ogni riavvio)" if SECRET_KEY_MANCANTE else "impostata",
        "impostata" if os.environ.get("ADMIN_TOKEN") else "ASSENTE (il pannello admin risponde 404)",
        "impostato" if os.environ.get("SMTP_HOST") else "assente (gli inviti si copiano a mano)",
        IN_PRODUZIONE))
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
