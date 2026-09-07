#!/usr/bin/env python3
"""
Backend multi-tenant per la piattaforma SaaS di gestione turni.

Fase 1 della roadmap: aggiunge il concetto di "azienda" (tenant) e un login
semplice (email + password) sopra la logica di generazione turni esistente,
che resta INVARIATA nel frontend (static/index.html) — l'unica differenza è
che ogni azienda ora legge/scrive il proprio stato, non uno stato condiviso
da tutti come nella versione locale del 9 Days Bar.

Ogni azienda ha un'unica riga di stato (tabella app_state) che contiene
l'intero blob JSON dei dati (impostazioni, personale, turni) esattamente
come faceva la versione precedente con id=1 fisso — cambia solo che ora
quella riga è per azienda (azienda_id) invece che globale, ed è protetta
da login: nessuno può leggere o scrivere lo stato di un'altra azienda,
perché l'azienda non viene mai presa da ciò che manda il client, ma sempre
dalla sessione autenticata sul server.

Avvio locale (usa SQLite, nessuna configurazione):
    pip install -r requirements.txt
    python app.py
    # poi crea la prima azienda pilota:
    flask --app app create-tenant

Deploy su Render: vedi README.md.
"""
import json
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, request, redirect, url_for, render_template, send_from_directory
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from werkzeug.security import generate_password_hash, check_password_hash

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

class Azienda(db.Model, UserMixin):
    """Un tenant: un'attività cliente della piattaforma.

    Per la fase 1 un'azienda = un login (email+password). Più utenti per la
    stessa azienda, con ruoli diversi, sono un'estensione naturale successiva
    (basterebbe una tabella Utente separata con azienda_id + ruolo), non
    urgente per i 2 clienti pilota.
    """
    __tablename__ = "aziende"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    creato_il = db.Column(db.DateTime, default=now_utc)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AppState(db.Model):
    """Il blob di stato di un'azienda: stessa struttura dati della versione
    locale (impostazioni, personale, turni), ma una riga per azienda invece
    che una sola riga globale. Il numero di versione mantiene lo stesso
    meccanismo di concorrenza ottimistica già usato dal frontend."""
    __tablename__ = "app_state"

    id = db.Column(db.Integer, primary_key=True)
    azienda_id = db.Column(db.Integer, db.ForeignKey("aziende.id"), unique=True, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=0)
    data = db.Column(db.Text)
    aggiornato_il = db.Column(db.DateTime, default=now_utc)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Azienda, int(user_id))


# --- pagine --------------------------------------------------------------

@app.route("/")
@login_required
def index():
    # L'app (static/index.html) è invariata rispetto alla versione locale:
    # legge/scrive /api/state esattamente come prima. Il login la protegge
    # a monte: si arriva qui solo con una sessione valida.
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        azienda = Azienda.query.filter_by(email=email).first()
        if azienda and azienda.check_password(password):
            login_user(azienda, remember=True)
            return redirect(url_for("index"))
        error = "Email o password non corrette."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --- API di stato (stesso contratto della versione locale, ma per-tenant) ---

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_utc().isoformat()})


@app.route("/api/whoami")
@login_required
def whoami():
    return jsonify({"nome": current_user.nome, "email": current_user.email})


@app.route("/api/state", methods=["GET"])
@login_required
def get_state():
    row = AppState.query.filter_by(azienda_id=current_user.id).first()
    if not row:
        return jsonify({"version": 0, "data": None})
    return jsonify({"version": row.version, "data": json.loads(row.data) if row.data else None})


@app.route("/api/state", methods=["PUT"])
@login_required
def put_state():
    payload = request.get_json(force=True, silent=True) or {}
    client_version = payload.get("version", 0)
    data = payload.get("data")
    if data is None:
        return jsonify({"error": "dati mancanti"}), 400

    # with_for_update blocca la riga finché la transazione non finisce, per
    # evitare corse tra richieste concorrenti sulla stessa azienda (su
    # SQLite, usato solo in locale, viene ignorato senza errore).
    row = db.session.execute(
        select(AppState).where(AppState.azienda_id == current_user.id).with_for_update()
    ).scalar_one_or_none()

    if not row:
        if client_version not in (0, None):
            db.session.rollback()
            return jsonify({"error": "conflitto", "version": 0, "data": None}), 409
        row = AppState(azienda_id=current_user.id, version=1, data=json.dumps(data), aggiornato_il=now_utc())
        db.session.add(row)
        db.session.commit()
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


# --- gestione aziende (onboarding pilota, da riga di comando) ---------------

@app.cli.command("create-tenant")
def create_tenant():
    """Crea una nuova azienda pilota: flask --app app create-tenant"""
    import getpass
    db.create_all()
    nome = input("Nome attività: ").strip()
    email = input("Email di accesso: ").strip().lower()
    password = getpass.getpass("Password: ")
    if Azienda.query.filter_by(email=email).first():
        print("Esiste già un'azienda con questa email.")
        return
    azienda = Azienda(nome=nome, email=email)
    azienda.set_password(password)
    db.session.add(azienda)
    db.session.commit()
    print(f"Creata azienda '{nome}' ({email}), id={azienda.id}.")


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    print("Piattaforma turni — server multi-tenant")
    print(f"Apri http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=os.environ.get("FLASK_DEBUG") == "1")
