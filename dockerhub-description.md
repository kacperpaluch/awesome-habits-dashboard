# Awesome Habits Lens

Prywatny, self-hostowany dashboard analityczny dla eksportów CSV z Awesome Habits.

## Najważniejsze funkcje

- ręczny import CSV z przeglądarki,
- prywatny webhook do automatycznego przesyłania eksportów,
- skuteczność, streaki, trendy 7/30, heatmapa i idealne dni,
- bieżące dni i tygodnie oznaczane jako „w trakcie” bez zaniżania skuteczności,
- automatyczne, zweryfikowane backupy SQLite oraz bezpieczne przywracanie,
- obsługa nawyków dziennych, tygodniowych, Building i Breaking,
- filtry zakresów czasu, nawyków, list oraz okresów,
- lokalny zapis w SQLite i trwały named volume,
- obrazy dla AMD64 i ARM64,
- brak zewnętrznych usług i zależności runtime.

## Szybki start

```yaml
services:
  awesome-habits-dashboard:
    image: kpa90/awesome-habits-dashboard:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Warsaw
      MAX_UPLOAD_MB: 20
      MAX_BACKUP_MB: 100
      BACKUP_TIME: "03:00"
      BACKUP_KEEP: 14
      WEBHOOK_TOKEN: ""
    volumes:
      - awesome-habits-dashboard-data:/app/data

volumes:
  awesome-habits-dashboard-data:
```

Po uruchomieniu otwórz `http://localhost:8080`. Gdy `WEBHOOK_TOKEN` jest pusty, bezpieczny token zostanie wygenerowany automatycznie i zapisany w wolumenie. Gotowy adres webhooka jest widoczny w ustawieniach aplikacji.

Eksport możesz wysłać również automatycznie:

```bash
curl -X POST "https://dashboard.example.com/webhook/TWOJ_TOKEN" \
  -H "Content-Type: text/csv" \
  --data-binary @AwesomeHabits.csv
```

Przy publicznym wdrożeniu użyj HTTPS i zabezpiecz dashboard przez reverse proxy, VPN lub Cloudflare Access.
