# Kontekst techniczny

## Cel

Awesome Habits Lens jest samodzielnym dashboardem dla pełnych eksportów CSV z Awesome Habits. Import jest snapshotem: nowy poprawny plik zastępuje wszystkie aktywne rekordy, natomiast historia metadanych importów pozostaje w tabeli `imports`.

## Stack

- Python 3.12 i wyłącznie biblioteka standardowa,
- `ThreadingHTTPServer` jako serwer HTTP,
- SQLite z WAL,
- frontend bez frameworka w `static/` (plus `manifest.json` i ikony PWA),
- kontener Alpine naprawiający przy starcie uprawnienia trwałego wolumenu, a następnie uruchamiający aplikację jako użytkownik bez uprawnień root.

## Przepływ importu

1. `POST /api/import` przyjmuje multipart z polem `file`.
2. `POST /webhook/{token}` przyjmuje ten sam multipart lub surowy CSV.
3. Cały plik jest walidowany przed otwarciem transakcji.
4. W transakcji zapisywany jest wpis `imports`, usuwany poprzedni snapshot i wstawiane nowe rekordy.
5. Dashboard czyta tylko najnowszy snapshot z `records`.
6. Niezależny wątek tworzy raz dziennie po `BACKUP_TIME` zweryfikowany backup SQLite z retencją `BACKUP_KEEP`, jeśli baza zawiera dane.

Każda poprawna i odrzucona próba przetworzenia CSV trafia do `imports` ze statusem i opcjonalnym błędem. `GET /api/imports` udostępnia historię stronicowaną i filtrowaną po datach; odrzucony import nie modyfikuje tabeli `records`.

Token webhooka pochodzi z `WEBHOOK_TOKEN`. Gdy zmienna jest pusta, powstaje losowy token `secrets.token_urlsafe(24)` zapisany w `WEBHOOK_TOKEN_FILE` (domyślnie obok bazy w wolumenie). Ścieżki webhooków są redagowane w logach HTTP.

## Semantyka danych

Pole `Status` z eksportu jest źródłem informacji o wykonaniu, ale niezakończony rekord z bieżącego dnia lub tygodnia otrzymuje stan `in_progress`. Nie obniża skuteczności ani trendu, dopóki jego okres się nie zakończy. `Type`, `Goal` i `Quantity` służą do prezentowania postępu i rekordów. Rekordy `Daily` budują heatmapę i idealne dni; `Weekly` mają osobne streaki z krokiem tygodniowym.

Backup używa online backup API SQLite, sprawdza integralność, schemat i SHA-256. Snapshot jest finalizowany z `journal_mode=DELETE` i nie wymaga plików `-wal`/`-shm`; osierocone sidecary w katalogu backupu są przy starcie scalane z plikiem bazy (`wal_checkpoint(TRUNCATE)`), nigdy kasowane, bo niosą zatwierdzone transakcje. Weryfikacja czyta backup jako `mode=ro` (bez `immutable`), więc widzi też dane z `-wal`, a wynik jest cache'owany po `mtime`/rozmiarze pliku. Harmonogram `BACKUP_TIME` (`HH:MM`, domyślnie `03:00`) działa według `TZ` kontenera i wykonuje najwyżej jedną kopię planową dziennie. Restore zawsze tworzy `awesome-habits-pre-restore-…db`, zanim zastąpi aktywną bazę; import tworzy `awesome-habits-pre-import-…db`, jeśli SHA-256 payloadu różni się od ostatniego udanego importu (bo `import_csv` robi `DELETE FROM records`). Backupy znajdują się domyślnie w `/app/data/backup`, więc pozostają w named volume.

## Frontend

Dwa breakpointy: 900 px i 620 px. Poniżej 620 px tabela nawyków renderuje się jako karty — `renderHabits` daje każdej `td` atrybut `data-label`, a CSS zamienia komórki na wiersze etykieta/wartość, więc nie ma osobnego szablonu dla telefonu. Tryb ciemny to warstwa nadpisań `@media(prefers-color-scheme:dark)` na końcu `styles.css`; wykres trendu czyta kolory z custom properties, dlatego przełączenie motywu wymusza `loadDashboard`. Poniżej 520 px szerokości canvasu trend rysuje wyłącznie średnie kroczące, a serie są decymowane do ~1,5 punktu na piksel. Nasłuch `resize` reaguje tylko na zmianę szerokości, bo na mobile chowanie paska adresu zmienia samą wysokość. Kopiowanie webhooka ma fallback na `execCommand`, ponieważ `navigator.clipboard` nie istnieje poza secure context, czyli przy wejściu po HTTP z adresu w sieci lokalnej.

## Weryfikacja

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
docker build -t awesome-habits-dashboard:test .
```
