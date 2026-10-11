# Disclaimer / Отказ от ответственности

## 1. Use at your own risk / Используйте на свой страх и риск

**English.** This repository is an independent interoperability and research
project. It is not affiliated with, endorsed by, or authorized by NCSOFT or the
AION 2 publishers. The software is provided without warranty. Running traffic
capture, keeping a game session open, querying a market, or exposing the local
site may violate local law, organizational policy, or game/account agreements in
some jurisdictions. The operator accepts all risk, including account sanctions,
data loss, service interruption, and security exposure.

**Русский.** Это независимый исследовательский проект по совместимости. Он не
связан с NCSOFT или издателями AION 2, не одобрен и не авторизован ими. Программа
поставляется без гарантий. В отдельных юрисдикциях или сетях захват трафика,
сохранение игровой сессии, чтение рынка либо публикация локального сайта могут
нарушать закон, внутренние правила или пользовательское соглашение. Пользователь
запускает проект на свой страх и риск и принимает возможные последствия, включая
санкции аккаунта, потерю данных, простой и раскрытие локального сервиса.

## 2. Current build is not release-verified / Текущая сборка не проверена как релиз

**English.** The current public Docker assembly has not completed a clean
end-to-end release acceptance on an independent machine. You should expect to
debug, adapt, and finish it yourself. The core market parsing pipeline has worked
in the authors' research environment across dozens of parsing runs, including
all 100 configured Global market scopes, but this does not guarantee that the
published checkout, your client build, or future protocol versions will work.
The bundled database is the complete credential-free derived market database
available to the authors at publication time. It contains historical summary
observations and collected listing rows, but it is not live data and cannot be
a complete archive of every listing that existed between scans.

**Русский.** Текущая публичная Docker-сборка не прошла чистую сквозную приёмку
на независимой машине. Будьте готовы самостоятельно исправлять, адаптировать и
доделывать её. Ядро рыночного парсинга работало в исследовательском контуре
авторов на десятках запусков, включая обход всех 100 настроенных Global market
scopes, но это не гарантирует работу опубликованного checkout, вашей версии
клиента или будущих версий протокола. Встроенная БД содержит все безопасные
производные рыночные данные, имевшиеся у авторов на момент публикации: историю
сводных цен и собранные строки лотов. Это не live-цены и не архив каждого лота,
существовавшего между сканированиями.

## Data and trademarks

AION, AION 2, NCSOFT, item names, artwork, and related marks or game data belong
to their respective owners. The Apache-2.0 license applies only to project-owned
source code. It does not grant rights to game assets or client-derived data.
