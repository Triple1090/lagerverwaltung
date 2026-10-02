# Lagerverwaltung

Eigenständiger Scan-Dienst (FastAPI + Vanilla JS) für die Lagerverwaltung über InvenTree.
Ersetzt die vorherige n8n-basierte Variante.

Deployed via Dokploy als `lager-app-v2` unter `https://lager.leine-honig.de`.

## Stack

- FastAPI + Uvicorn
- InvenTree REST API als Backend
- Vanilla JS Frontend (kein Build-Step)
- Session-basierte Anmeldung via Basic-Auth-Zugangsdaten

## Lokale Entwicklung

```bash
cp .env.example .env
# INVENTREE_TOKEN, BASIC_AUTH_USER, BASIC_AUTH_PASS in .env setzen

pip install -r requirements.txt
uvicorn main:app --reload
```

Die App erwartet `index.html` und `login.html` im Arbeitsverzeichnis `/app`
(siehe `Dockerfile`).

## Deployment

```bash
docker compose up -d --build
```

Benötigte Umgebungsvariablen:

| Variable           | Beschreibung                          |
|--------------------|----------------------------------------|
| `INVENTREE_URL`    | Basis-URL der InvenTree-Instanz        |
| `INVENTREE_TOKEN`  | API-Token für InvenTree                |
| `BASIC_AUTH_USER`  | Benutzername für den Login             |
| `BASIC_AUTH_PASS`  | Passwort für den Login                 |
