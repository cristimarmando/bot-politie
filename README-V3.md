# C.C.O. Bot — Panel V3 Premium

Acest pachet este versiunea completă V3 peste botul Python V2.

## Ce s-a schimbat

Panelul are taburi separate pentru:
- Dashboard
- Membri
- Pontaje
- Callsign-uri
- Ticket-uri
- Testeri
- Warn-uri
- Security
- Backups
- Loguri
- Configurare
- Superuser

## Funcții importante

### Membri
- search după Discord ID / nume / prenume / ID intern / callsign
- filtre după status, grad și tester
- profil administrativ complet
- pontaj filtrabil pe perioade
- promovare / retrogradare / setare grad
- suspendare / reactivare / eliminare
- tester
- warn
- ajustări pontaj
- callsign direct din panel

### Pontaje
- azi / ieri / 7 zile / lună / total
- filtre după user și status
- total perioadă
- export CSV

### Callsign-uri
- intervale per grad
- vizualizare slot cu slot
- liber / ocupat / rezervat
- atribuire directă unui membru
- eliberare
- rezervare

### Security
- carduri ON/OFF
- praguri custom
- whitelist
- restore channels / roles
- snapshot interval

### Configurare
- rolurile și canalele se aleg din datele serverului Discord direct din panel

### Superuser
- Owner-ul poate adăuga/elimina Superuseri
- permisiuni pe module
- ultimul login în panel

## Fișiere principale modificate

- `cco/main.py`
- `cco/db.py`
- `cco/panel_bridge.py` (nou)
- `dashboard/app.py`
- `templates/dashboard.html`
- `static/style.css`
- `static/dashboard.js` (nou)

## Instalare

Păstrează `.env` din proiectul tău actual.

În folderul proiectului:

```powershell
python -m pip install -r requirements.txt
python run.py
```

Apoi:

```text
http://localhost:3000
```

## OAuth2

În Discord Developer Portal -> OAuth2 -> Redirects trebuie să existe exact:

```text
http://localhost:3000/auth/discord/callback
```

În `.env`:

```env
DASHBOARD_BASE_URL=http://localhost:3000
DISCORD_REDIRECT_URI=http://localhost:3000/auth/discord/callback
```

## Foarte important

`localhost` funcționează doar pe calculatorul care găzduiește panelul. Dacă vrei ca alți membri ai conducerii să intre de pe PC-urile lor, trebuie să pui dashboard-ul pe un URL public HTTPS (domeniu, VPS sau tunel securizat), iar `DASHBOARD_BASE_URL` și redirect-ul OAuth trebuie schimbate cu acel URL.

## Siguranță

Testează acțiunile administrative și Security pe un server Discord de test înainte de folosirea pe serverul principal.
