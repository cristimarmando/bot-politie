# C.C.O. Control Center V5

V5 extinde V4 cu protecții administrative și callsign-uri separate pe fiecare grad de Conducere.

## Logo personalizat

Pune logo-ul tău aici:

`static/logo.png`

Recomandat: PNG pătrat, de exemplu 512x512, cu fundal transparent. Dacă fișierul lipsește, panelul afișează automat fallback-ul `CCO`.

## Callsign-uri de Conducere pe grad

Fiecare grad are propriul pool:

- Coordonator Operațional
- Comandant Operațional
- Director Adjunct C.C.O.
- Director General C.C.O.

Exemple: `CAPITALA`, `VULTUR`, `OMEGA`, `TITAN` etc. Superuserul le adaugă și editează direct din Panel -> Callsign-uri.

Regulă la schimbarea gradului:
- 1 callsign liber: se atribuie automat;
- mai multe callsign-uri libere: se alege unul;
- 0 callsign-uri libere: operația se oprește.

## Protecții V5

Sunt incluse:

- Role Lock pentru rolurile sensibile/configurate;
- Role Permission Guard pentru Administrator / Manage Guild / Manage Roles / Ban / Kick etc.;
- Protected Rank Roles: rolurile C.C.O. adăugate/scoase manual sunt restaurate;
- Callsign Duplicate Guard prin validare + constrângeri DB;
- Command Permission Guard;
- Command Cooldown;
- Protected Users;
- Critical Confirmation pentru acțiuni periculoase din panel;
- Command Audit;
- Panel Session Security cu timeout configurabil;
- Panel Permission Matrix pentru Superuseri;
- Nickname/Role Restore Snapshot;
- Self-Elevation Log pentru Owner/Superuser;
- Nickname Guard: nickname-ul C.C.O. este restaurat automat și se scrie log;
- Role Snapshot Restore: rolurile adăugate/scoase manual membrilor C.C.O. pot fi restaurate și logate.

Pentru logurile de nickname/rol setează `log_roles` la canalul dorit din Panel -> Configurare sau cu `/config-canal`.

## Superuser self-management

Ownerul și Superuserii pot să își modifice propriul grad/promovarea/retrogradarea. Acțiunea este înregistrată separat ca `self_elevation` în audit. În panel, schimbările critice proprii cer `CONFIRM`.

## Instalare / upgrade din V4

1. Fă backup la `data/cco.sqlite` și `.env`.
2. Copiază fișierele V5 peste proiect.
3. Păstrează baza ta de date existentă.
4. Rulează:

```powershell
python -m pip install -r requirements.txt
python run.py
```

Migrațiile V5 adaugă automat coloana `rank` pentru callsign-urile de Conducere și tabelele de protecție.

Dacă ai callsign-uri de Conducere vechi din V4 fără grad, ele apar în panel la `NECONFIGURAT / MIGRAT V4`; editează fiecare și alege gradul corect.

## Setări recomandate

În Security Center lasă activate:

- `nickname_protection`
- `role_lock_enabled`
- `role_permission_guard`
- `protected_rank_roles`
- `callsign_duplicate_guard`
- `command_permission_guard`
- `command_cooldown_enabled`
- `protected_users_enabled`
- `role_escalation_protection`
- `member_role_restore`

`panel_session_minutes` este implicit 30.

## Important Discord

Botul trebuie să aibă rolul poziționat deasupra rolurilor pe care trebuie să le gestioneze și permisiunile necesare: Manage Roles, Manage Nicknames, View Audit Log, Kick/Ban/Moderate Members și restul permisiunilor folosite de proiect.
