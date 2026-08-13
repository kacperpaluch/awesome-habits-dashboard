# Awesome Habits Lens

[![Docker Hub](https://img.shields.io/docker/pulls/kpa90/awesome-habits-dashboard?logo=docker)](https://hub.docker.com/r/kpa90/awesome-habits-dashboard)

Self-hostowany dashboard analityczny dla eksportów CSV z aplikacji Awesome Habits. Nie wymaga API ani zewnętrznej bazy danych — plik można załadować w interfejsie albo automatycznie wysłać na prywatny webhook.

## Funkcje

- import eksportu `AwesomeHabits.csv` metodą drag & drop,
- prywatny webhook do automatyzacji importu,
- atomowa podmiana danych — błędny plik nie niszczy poprzedniego snapshotu,
- nawyki dzienne i tygodniowe, Building i Breaking,
- trzy stany okresu: wykonane, niewykonane i „w trakcie” dla bieżącego dnia/tygodnia,
- skuteczność bez zaniżania wyniku przez trwające okresy, streaki, idealne dni, trend 7/30 oraz heatmapa,
- automatyczny backup o wybranej godzinie, backup na żądanie i bezpieczne przywracanie,
- historia importów, webhooków i backupów z filtrem dat oraz paginacją,
- filtrowanie według nawyku, listy, okresu i zakresu dat,
- responsywny interfejs oraz szczegóły każdego nawyku,
- lokalna baza SQLite w trwałym wolumenie,
- brak zależności runtime poza biblioteką standardową Pythona.

## Uruchomienie

Obraz jest dostępny na [Docker Hub](https://hub.docker.com/r/kpa90/awesome-habits-dashboard) dla `linux/amd64` i `linux/arm64`.

```bash
cp .env.example .env
docker compose up -d
```

Dashboard będzie dostępny pod adresem <http://localhost:8080>.

Jeśli `WEBHOOK_TOKEN` pozostanie pusty, aplikacja przy pierwszym uruchomieniu wygeneruje losowy token i zachowa go w named volume. Pełny URL webhooka znajdziesz w ustawieniach dashboardu. Możesz również jawnie ustawić stały, długi token w `.env`:

```env
WEBHOOK_TOKEN=tu-wstaw-dlugi-losowy-ciag
```

### Portainer

1. Utwórz Stack i wklej `docker-compose.yml`.
2. Opcjonalnie ustaw `APP_PORT`, `WEBHOOK_TOKEN`, `MAX_UPLOAD_MB`, `BACKUP_TIME` i `TZ`.
3. Wdróż Stack.
4. Otwórz dashboard i skopiuj adres webhooka z ustawień.

Dane SQLite i automatycznie wygenerowany token pozostaną w wolumenie `awesome-habits-dashboard-data`.
Przy starcie kontener automatycznie naprawia właściciela katalogu danych utworzonego przez Docker lub Portainera, a następnie uruchamia aplikację jako nieuprzywilejowany użytkownik `habits`.

### Backup i przywracanie

Aplikacja tworzy raz dziennie zweryfikowany snapshot SQLite w podkatalogu `backup` tego samego wolumenu. Godzinę ustawia `BACKUP_TIME` w formacie `HH:MM` (domyślnie `03:00`), według strefy `TZ`. Backup powstaje przy pierwszym sprawdzeniu po tej godzinie, o ile baza zawiera dane. W ustawieniach aplikacji można również:

- utworzyć backup na żądanie,
- pobrać wybraną kopię `.db`,
- przywrócić kopię serwerową,
- przywrócić bazę z przesłanego pliku `.db`.

Przed każdym przywróceniem aplikacja automatycznie tworzy kopię `pre-restore`. Backup jest akceptowany dopiero po sprawdzeniu nagłówka SQLite, `PRAGMA integrity_check`, wymaganych tabel i sumy SHA-256. Przywrócenie wymaga wpisania `PRZYWRÓĆ`.

Domyślnie zachowywanych jest 14 najnowszych kopii. Zmienisz to przez `BACKUP_KEEP`; limit przesyłanego backupu ustawia `MAX_BACKUP_MB`. Kopie w wolumenie chronią przed błędnym importem lub przywróceniem, ale dla ochrony przed utratą hosta warto regularnie pobierać plik `.db` poza serwer.

Każdy snapshot `.db` jest finalizowany jako samodzielny plik. `habits.db-wal` i `habits.db-shm` obok **aktywnej** bazy są normalnymi plikami roboczymi SQLite. Gdyby po nagłym zatrzymaniu kontenera taki sidecar został przy pliku w katalogu `backup/`, aplikacja przy starcie scala go z bazą (`wal_checkpoint`) zamiast kasować — plik `-wal` może zawierać zatwierdzone dane, których nie ma jeszcze w pliku głównym.

## Import danych

W Awesome Habits wykonaj eksport CSV, a następnie przeciągnij plik na dashboard lub wybierz go przez przycisk „Importuj CSV”. Importowany plik jest kompletnym snapshotem i zastępuje poprzedni zestaw rekordów.

Plik `AwesomeHabits.csv` jest ignorowany przez Git, aby prywatne dane nie trafiły przypadkiem do publicznego repozytorium.

Wymagane kolumny:

```text
Date, Name, Period, Type, Goal, Quantity, Status
```

Obsługiwane są także pola `Description`, `Archived`, `Unit`, `Lists` i `Note`. Plik powinien używać kodowania UTF-8; BOM jest akceptowany.

### Webhook

Wyślij eksport jako surowe body:

```bash
curl -X POST "https://subdomena.example.com/webhook/TWOJ_TOKEN" \
  -H "Content-Type: text/csv" \
  --data-binary @AwesomeHabits.csv
```

Albo jako multipart:

```bash
curl -X POST "https://subdomena.example.com/webhook/TWOJ_TOKEN" \
  -F "file=@AwesomeHabits.csv;type=text/csv"
```

Token w ścieżce jest sekretem. Przy publicznym wdrożeniu użyj HTTPS i dodatkowo zabezpiecz sam dashboard przez Cloudflare Access, VPN lub uwierzytelnianie reverse proxy.

W „Źródle danych” znajduje się paginowana historia poprawnych i odrzuconych webhooków/importów. Pokazuje czas, źródło, plik, zakres danych, liczbę rekordów, informację o zmianie snapshotu albo komunikat błędu. Filtr „Od–Do” pozwala ograniczyć historię bez długiego scrollowania. Odrzuconych wpisów przechowywanych jest maksymalnie 50 — dzięki temu seria błędnych żądań nie rozdmuchuje bazy.

Reverse proxy powinno przekazywać nagłówki `Host`, `X-Forwarded-Host` i `X-Forwarded-Proto`, aby interfejs wyświetlał prawidłowy publiczny URL webhooka.

## Uruchomienie lokalne

```bash
python3 app.py
```

Aplikacja utworzy bazę w `data/habits.db`.

## API

| Metoda | Endpoint | Opis |
|---|---|---|
| `GET` | `/api/health` | Healthcheck |
| `GET` | `/api/config` | Konfiguracja importu i URL webhooka |
| `GET` | `/api/dashboard` | Dane dashboardu, filtry w query string |
| `GET` | `/api/habits/{name}` | Szczegóły nawyku |
| `POST` | `/api/import` | Import multipart z interfejsu |
| `POST` | `/webhook/{token}` | Import surowego CSV lub multipart |
| `GET` | `/api/imports` | Paginowana historia importów i webhooków |
| `GET` | `/api/backups` | Status i lista zweryfikowanych kopii |
| `POST` | `/api/backup` | Utworzenie backupu na żądanie |
| `GET` | `/api/backups/{file}/download` | Pobranie wybranej kopii |
| `POST` | `/api/backups/{file}/restore` | Przywrócenie kopii serwerowej |
| `POST` | `/api/backups/restore-upload` | Przywrócenie przesłanej bazy |

Endpointy historii przyjmują `page`, `per_page`, `date_from` i `date_to`.

## Testy

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```

## Rollback obrazu

Każde wydanie otrzymuje tag `latest` i stały tag z krótkim SHA commita. Aby wrócić do konkretnej wersji, zmień tag obrazu w `docker-compose.yml`, a następnie wykonaj `docker compose up -d`.
