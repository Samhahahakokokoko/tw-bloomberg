#!/usr/bin/env bash
# 手動執行一次爬蟲
cd "$(dirname "$0")"
python -m scraper.news_scraper
