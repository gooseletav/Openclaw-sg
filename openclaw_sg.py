yaml
name: OpenClaw-SG Scanner

on:
  workflow_dispatch:
  schedule:
    - cron: '0 */2 * * *'

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3

      - name: Python setup
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install deps
        run: |
          pip install requests feedparser python-dotenv

      - name: Run bot
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}
        run: |
          python openclaw_sg.py
