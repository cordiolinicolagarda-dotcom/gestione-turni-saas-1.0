# Gestione turni — piattaforma multi-cliente (fase 1)

Fase 1 della roadmap: multi-tenant minimo. La logica di generazione turni
(`static/index.html`) è la stessa, testata, del 9 Days Bar — non è stata
riscritta. Quello che è nuovo è tutto nel backend (`app.py`):

- ogni attività cliente ("azienda") ha un proprio account (email + password)
  e vede solo i propri dati — impostazioni, personale, turni;
- l'accesso è protetto da login (Flask-Login);
- i dati sono in PostgreSQL in produzione (SQLite in locale, senza bisogno
  di installare nulla).

## Avvio in locale

```
pip install -r requirements.txt
python app.py
```

Poi, in un secondo terminale, crea la prima azienda di prova:

```
flask --app app create-tenant
```

Segui le domande (nome attività, email, password), poi apri
`http://localhost:5000` e accedi con quelle credenziali. L'app si comporta
esattamente come la versione locale del 9 Days Bar, ma il salvataggio va
sul database, ed è visibile solo a chi accede con quell'account.

Per creare una seconda azienda (es. il secondo cliente pilota), ripeti
`flask --app app create-tenant`: i dati restano completamente separati.

## Deploy su Render

1. Crea un repository Git con questa cartella e mettilo su GitHub (Render
   fa il deploy da un repo collegato).
2. Su [render.com](https://render.com), "New" → "Blueprint", collega il
   repository: il file `render.yaml` incluso qui crea automaticamente sia
   il servizio web che il database Postgres, e li collega da soli
   (`DATABASE_URL` viene impostata in automatico).
3. Al primo deploy il database è vuoto: apri la shell del servizio da
   Render ("Shell" nel pannello del servizio web) ed esegui
   `flask --app app create-tenant` per creare gli account dei due clienti
   pilota, con le stesse domande di sopra.
4. Collega il tuo dominio da Render → Settings → Custom Domain: Render
   fornisce HTTPS automatico una volta puntato il DNS.

## Cosa NON c'è ancora (di proposito, fasi successive)

- Reparti configurabili (oggi cucina/banco/sala sono ancora fissi nella
  logica di generazione, come nella versione originale) — fase 2.
- Registrazione pubblica self-service (per ora le aziende si creano da
  riga di comando: con 2 clienti pilota noti non serve un form pubblico,
  ed evita di esporre un endpoint di registrazione prima di averne
  bisogno davvero).
- Fatturazione/abbonamento (Stripe o simile) — fase 5, a validazione
  avvenuta.
- Più utenti per la stessa azienda con ruoli diversi (oggi un account =
  un login) — estensione naturale quando servirà.
