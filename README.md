# easyJet Live News Dashboard

TV-friendly live news dashboard for the official easyJet Media Centre, with one current Times of India Health story and a final cybersecurity-awareness slide.

## What it does

- Collects the latest easyJet Media Centre stories from official easyJet pages.
- Keeps the latest 30 days of easyJet stories.
- Uses the article page's lead image/description when available.
- Shows one story per slide for 30 seconds and loops automatically.
- Displays a live slide counter such as `1 / 48`.
- Adds exactly one current health story from the official Times of India Health section: https://timesofindia.indiatimes.com/health
- Places a cybersecurity-awareness slide at the end, styled to match the dashboard rather than using the supplied reference image directly.
- Dashboard data refreshes every 60 seconds; easyJet source polling is every 15 minutes.
- Keeps the existing easyJet-first slideshow order, followed by Health, then Security Awareness.

## Sources

- Official easyJet Media Centre: https://www.easyjet.com/en/news/stories
- Times of India Health: https://timesofindia.indiatimes.com/health

The Times of India health item is fetched dynamically and only one current article is shown. If the external health page temporarily blocks the Render server, the dashboard continues to run with the easyJet and security slides.

## Deployment

Docker-ready for Render. SQLite is used for easyJet article state. Render Free storage is ephemeral, so persistent storage/database should be considered for production.
