# HZZ Zadar Job Bot

Prati službeni RSS feed [Burze rada HZZ-a](https://burzarada.hzz.hr) za **Zadarsku županiju** i šalje Telegram poruku kad se pojavi **novi oglas s mjestom rada Zadar**.

Ostala mjesta u županiji (Petrčane, Bibinje, Poličnik, Biograd, …) se **ignoriraju**.

**Preporučeni način rada:** GitHub Actions (cron), ne laptop. Računalo ne mora biti upaljeno.

| | |
|---|---|
| Izvor | https://burzarada.hzz.hr/rss/rsszup20.xml |
| Filter | `Mjesto rada = Zadar` (case-insensitive) |
| Provjera | ~**00:00** i ~**12:00** (Europe/Zagreb), plus ručno |
| Pamćenje | `seen_jobs.json` (WebSifra) — commita se natrag u repo |

---

## 1. Telegram bot token i chat_id

### 1.1 Token od BotFather

1. Otvori Telegram → **[@BotFather](https://t.me/BotFather)**.
2. Pošalji `/newbot`.
3. Upiši ime (npr. `HZZ Zadar oglasi`) i username koji završava na `bot`.
4. Dobiješ token: `123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
5. Token čuvaj kao lozinku.

### 1.2 Chat ID

1. Otvori **svog** novog bota i pošalji `/start`.
2. Potraži **[@userinfobot](https://t.me/userinfobot)** → `/start` → uzmi `Id`.

Ili u pregledniku (zamijeni `TOKEN`):

```text
https://api.telegram.org/botTOKEN/getUpdates
```

U JSON-u potraži `"chat":{"id": 123456789`.

Za **grupu**: dodaj bota u grupu, pošalji poruku, pa uzmi `chat.id` (često negativan, npr. `-100123…`).

---

## 2. GitHub Actions (preporučeno)

GitHub runner svakih ~12 sati pokrene `python zadar_job_bot.py --once`, pošalje nove oglase na Telegram i spremi viđene ID-ove natrag u `seen_jobs.json`.

### 2.1 Secrets

U GitHub repou:

**Settings → Secrets and variables → Actions → New repository secret**

Dodaj točno ova dva imena:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token od BotFather |
| `TELEGRAM_CHAT_ID` | tvoj chat id |

Bez navodnika, bez razmaka.

### 2.2 Prvo pokretanje (ručno)

1. Kartica **Actions** u repou.
2. Lijevo: **Provjera HZZ oglasa (Zadar)**.
3. **Run workflow** → Run workflow.
   - Ostavi *Pošalji i trenutno aktivne oglase* **isključeno** (inače stigne 100+ poruka).
4. U Telegramu treba stići: *HZZ Zadar bot je pokrenut*.
5. Od sljedećeg crona (00:00 / 12:00) dolaze samo **novi** oglasi.

Ako želiš odmah trenutne oglase za Zadar, u Run workflow uključi tu kvačicu.

### 2.3 Raspored

Workflow se pali u **00:00 i 12:00 po Zagrebu**. GitHub cron je u UTC, pa su u YAML-u i ljetna i zimska vremena; isti oglas se ne šalje dvaput jer ID ide u `seen_jobs.json`.

GitHub ponekad zakasni 5–15 minuta. To za oglase za posao nije problem.

Ako repo **60 dana** nema aktivnosti, GitHub može ugasiti scheduled workflow. Otvori repo ili pokreni workflow ručno pa se ponovo aktivira.

### 2.4 Ako run padne

Bot pošalje Telegram poruku *GitHub Actions run nije uspio*. Log je u **Actions** tabu.

---

## 3. Kako izgleda obavijest

```text
🔔 Novi oglas za posao u Zadru

📌 Naslov: TRANSPORTNI RADNIK/TRANSPORTNA RADNICA
📍 Mjesto rada: ZADAR, ZADARSKA ŽUPANIJA
🏢 Poslodavac: ČISTOĆA, usluge održavanja čistoće d.o.o.
📅 Rok za prijavu: 29.8.2026.
🗂 Kategorija: STRUČNJACI U PROMETU

🔗 Otvori oglas na Burzi rada
```

Naslov i poslodavac se čitaju sa stranice oglasa jer RSS često ima prazan `<title>` i nema poslodavca. RSS je u `iso-8859-2` (HTTP header često laže da je UTF-8).

---

## 4. Lokalno pokretanje (opcionalno)

Ne pokreći lokalni scheduler **istovremeno** s GitHub Actions — dobile bi se duple obavijesti.

```bash
cd ~/bots/hzz-zadar-job-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python3 -m pip install -r requirements.txt
cp .env.example .env               # upiši TOKEN i CHAT_ID
python3 zadar_job_bot.py --test
python3 zadar_job_bot.py --once    # jedna provjera
```

| Naredba | Što radi |
|---|---|
| `python3 zadar_job_bot.py --once` | Jedna provjera i izlaz (isto što radi Actions) |
| `python3 zadar_job_bot.py --test` | Testna Telegram poruka |
| `python3 zadar_job_bot.py --once --send-existing` | Pošalji i trenutno aktivne oglase |
| `python3 zadar_job_bot.py --once --dry-run` | Bez Telegrama, samo log |
| `python3 zadar_job_bot.py` | Lokalna petlja 00:00 / 12:00 (laptop mora biti budan) |

macOS launchd / Linux cron / Windows Task Scheduler i dalje su u starijoj verziji uputa ispod, ako baš želiš laptop umjesto GitHuba.

<details>
<summary>Laptop: launchd / cron / Task Scheduler</summary>

### macOS (launchd)

```bash
mkdir -p ~/Library/LaunchAgents
PROJECT="$(pwd)"
sed "s|__PROJECT_DIR__|${PROJECT}|g" launchd/com.hzz.zadar-jobs.plist \
  > ~/Library/LaunchAgents/com.hzz.zadar-jobs.plist
launchctl load ~/Library/LaunchAgents/com.hzz.zadar-jobs.plist
```

### Linux (cron)

```cron
TZ=Europe/Zagreb
0 0,12 * * * /home/TVOJE_IME/bots/hzz-zadar-job-bot/.venv/bin/python /home/TVOJE_IME/bots/hzz-zadar-job-bot/zadar_job_bot.py --once >> /home/TVOJE_IME/bots/hzz-zadar-job-bot/logs/cron.log 2>&1
```

### Windows (Task Scheduler)

Program: `.venv\Scripts\python.exe`  
Arguments: `zadar_job_bot.py --once`  
Start in: mapa projekta  
Trigger: svakih 12 sati od 00:00.

</details>

---

## 5. Kako radi

1. Preuzme RSS Zadarske županije (`rsszup20.xml`).
2. Dekodira XML kao `iso-8859-2`.
3. Iz linka izvuče `WebSifra` (jedinstveni ID).
4. Ako mjesto rada nije Zadar → preskoči.
5. Ako je Zadar ili je RSS odrezan → otvori stranicu oglasa (naslov, poslodavac, rok).
6. Ako je ID nov → Telegram, pa ID u `seen_jobs.json`.
7. Na GitHubu, ako se lista ID-ova promijenila, Actions napravi commit.

Oglas se **ne** označi kao viđen ako slanje na Telegram nije uspjelo.

---

## 6. Greške

| Simptom | Što provjeriti |
|---|---|
| Workflow: *Dodaj TELEGRAM_BOT_TOKEN* | Settings → Secrets and variables → Actions |
| Telegram 401 | Krivi token (BotFather `/token`) |
| Telegram 400 / chat not found | Nisi poslao `/start` botu, ili krivi `CHAT_ID` |
| RSS timeout | Burza rada privremeno nedostupna; idući run će ponoviti |
| Nema obavijesti, a oglas postoji | Mjesto rada nije točno „Zadar“ (npr. Sukošan) |
| Dupli oglasi | Radi i laptop i Actions; ili je `seen_jobs.json` resetiran |
| Cron se više ne pali | 60 dana bez aktivnosti na repou — pokreni workflow ručno |

---

## 7. Privatnost

- **Ne committaj** `.env`. Token ide samo u GitHub Secrets.
- `seen_jobs.json` **jest** u gitu (samo ID-ovi oglasa, da Actions pamti stanje).
- Repo je najbolje držati **private**.
