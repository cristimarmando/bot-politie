# C.C.O. Bot + Panel V4

Versiunea V4 adaugă sistemul complet de membri, callsign-uri de Conducere, Activitate Operativă, cereri, sancțiuni și inactivitate.

## Migrare din V3

1. Oprește botul.
2. Fă backup la folderul actual, în special `data/cco.sqlite` și `.env`.
3. Copiază fișierele V4 peste proiectul tău.
4. Păstrează `.env` existent.
5. Păstrează `data/cco.sqlite` existent. La pornire, V4 creează automat tabelele și coloanele noi.
6. Rulează:

```powershell
python -m pip install -r requirements.txt
python run.py
```

## Roluri noi de configurat

Creează pe Discord rolurile dorite și configurează-le cu `/config-rol`:

```text
role_inactive       -> rolul de Inactivitate
role_av_1_1         -> AV 1/1
role_fw_1_3         -> FW 1/3
role_fw_2_3         -> FW 2/3
role_fw_3_3         -> FW 3/3
```

Botul trebuie să aibă rolul său deasupra acestor roluri.

La FW 3/3 membrul primește stadiul 3/3, apoi este trecut automat OUT, i se eliberează callsign-ul, i se scot rolurile C.C.O. și primește kick.

## Canale noi

Configurează câte un canal separat:

```text
/config-canal cheie:log_buletine
/config-canal cheie:log_filaje
/config-canal cheie:log_interogatorii
/config-canal cheie:log_actiuni
/config-canal cheie:log_cereri_actiune
/config-canal cheie:log_demisie
/config-canal cheie:log_inactivitate
/config-canal cheie:log_sanctiuni
```

Se pot configura și direct din Panel -> Configurare.

## Callsign-uri

### Grade normale

Primele 6 grade folosesc intervale numerice, de exemplu:

```text
/set-callsign-range grad:Detectiv prefix:D start:100 stop:199
```

### Conducere

Gradele de la `Coordonator Operațional` în sus folosesc callsign-uri text dintr-un pool comun.

Exemple:

```text
CAPITALA
VULTUR
OMEGA
BUCURESTI
```

Acestea se adaugă / editează / șterg din Panel -> Callsign-uri și numai Owner/Superuser poate modifica lista.

Când cineva intră în Conducere:
- 0 callsign-uri libere: operația este oprită;
- 1 callsign liber: este atribuit automat;
- mai multe: botul/panelul cere alegerea unuia dintre cele libere.

Dacă membrul este deja în Conducere și este promovat în interiorul Conducerii, își păstrează callsign-ul text.

## Comenzi noi V4

```text
/adauga-membru username nume prenume id grad
/adauga-buletin username nume_prenume cnp organizatie raport dovada
/adauga-filaj username participanti locatie raport dovada
/adauga-interogatoriu ...
/cerere-actiune username tip_actiune ora
/adauga-actiune coordonatori participanti tip_actiune ora dovada_fisier/dovada_link
/demisie username motiv precizari pk
/cerere-inactivitate username timp motiv
/sanctiune autor username_sanctionat sanctiune motiv zile dovada_fisier/dovada_link
```

Pentru `participanti` / `coordonatori` poți lipi mai multe mențiuni Discord în același câmp, de exemplu:

```text
@User1 @User2 @User3
```

## Activitate Operativă

Buletinele, filajele, interogatoriile și acțiunile intră în status `pending`. Conducerea le poate accepta sau respinge din mesajul Discord sau în bulk din Panel -> Activitate Operativă.

Numai înregistrările `accepted` sunt numărate în profilul membrului.

Profilul afișează:

```text
Buletine
Filaje
Interogatorii
Acțiuni
Pontaj
Sancțiuni
Istoric
```

Aceste valori sunt informative. Ele NU blochează promovarea; Conducerea poate promova când dorește, respectând ierarhia și disponibilitatea callsign-urilor.

## Cerere Acțiune

`Șef Serviciu Investigații` trebuie să folosească `/cerere-actiune`.

După acceptare, cererea este valabilă în intervalul de o oră înainte și o oră după ora solicitată. Exemplu: 15:00 -> 14:00-16:00.

Cererea poate fi folosită o singură dată pentru crearea unei acțiuni.

Conducerea poate crea acțiuni direct.

## Inactivitate

După acceptare:
- membrul primește `role_inactive`;
- statusul din panel devine `inactive`;
- este memorată data expirării;
- la expirare botul scoate automat rolul și revine la status `active`.

## Sancțiuni

Sistemul este:

```text
AV 1/1
FW 1/3
FW 2/3
FW 3/3 -> OUT automat
```

Durata implicită este 7 zile dacă nu se specifică alta.

Sancțiunile expirate rămân în istoric, dar rolul activ este recalculat automat.

## Demisie

Demisia este trimisă pe canalul configurat și trebuie acceptată sau respinsă.

Dacă solicitantul este membru al Conducerii, un membru obișnuit al Conducerii de grad egal sau inferior nu o poate accepta. Este necesar un grad superior; Owner/Superuser are bypass administrativ.

La acceptare:
- callsign-ul este eliberat;
- rolurile C.C.O. sunt eliminate;
- statusul devine `demisionat`;
- membrul primește kick;
- istoricul rămâne în baza de date.

## Pontaj

La anularea unui pontaj NU se mai trimite un mesaj separat. Mesajul original este editat și păstrează Clock In / Clock Out / durată, apoi adaugă cine l-a anulat.

## Panel V4

Taburi principale:

```text
Dashboard
Membri
Pontaje
Activitate Operativă
Callsign-uri
Ticket-uri
Testeri
Sancțiuni
Inactivități
Demisii
Security
Backups
Loguri
Configurare
Superuser
```

Conducerea poate intra în panel prin OAuth și are acces la modulele operaționale. Configurarea sensibilă și managementul de Superuser rămân protejate.
