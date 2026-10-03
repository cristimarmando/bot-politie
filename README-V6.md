# C.C.O. Control Center V6

V6 pornește de la V5 și modifică sistemul de sancțiuni și permisiunea pentru `/cerere-actiune`.

## Modificări V6

### `/cerere-actiune`
Comanda poate fi folosită numai de un membru care are în baza de date gradul exact **Șef Serviciu Investigații**. Owner/Superuser nu o pot folosi dacă nu au acest grad; Conducerea poate crea direct acțiuni prin `/adauga-actiune`.

### Sancțiuni
- Toate sancțiunile AV și FW durează exact **7 zile**. Parametrul `zile` a fost eliminat din `/sanctiune`.
- Primul AV activ = **AV 1/1**.
- Dacă membrul are deja un AV activ și primește încă un AV, noul AV este transformat automat în **FW**.
- FW-urile active merg **FW 1/3 → FW 2/3 → FW 3/3**.
- La **FW 3/3** se execută OUT automat, se eliberează callsign-ul, se scot rolurile C.C.O. și membrul primește kick.
- La expirarea unei sancțiuni, rolurile AV/FW sunt resincronizate automat.
- În DB se salvează și `requested_type`, astfel încât panelul poate afișa clar cazurile **AV → FW**.

## Upgrade din V5
1. Oprește botul.
2. Fă backup la `data/cco.sqlite` și `.env`.
3. Copiază fișierele V6 peste proiectul tău.
4. Păstrează baza de date existentă. Migrarea adaugă automat coloana `sanctions.requested_type`.
5. Rulează:

```powershell
python -m pip install -r requirements.txt
python run.py
```

## Roluri de sancțiuni necesare
Configurează în continuare:
- `role_av_1_1`
- `role_fw_1_3`
- `role_fw_2_3`
- `role_fw_3_3`

și canalul:
- `log_sanctiuni`
