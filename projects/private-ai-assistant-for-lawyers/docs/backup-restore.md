# 💾 Бэкап и восстановление

## Что бэкапить

| Что | Где | Размер |
|---|---|---|
| Код проекта | `/opt/rag_project/*.py`, `.env` | ~100 КБ |
| SQLite БД | `/opt/rag_project/history.db` | ~10 КБ |
| Документы | `/opt/rag_project/user_documents/` | зависит от загрузок |
| Qdrant volume | `rag_project_qdrant_data` | ~90 МБ |
| Конфиги | Nginx, systemd, Let's Encrypt | ~50 КБ |
| Скриншоты | `/opt/rag_project/static/` | ~1 МБ |
| **Не бэкапим:** Ollama volume | `rag_project_ollama_data` | 9 ГБ (скачается заново) |
| **Не бэкапим:** venv | `/opt/rag_project/venv/` | переустанавливается pip |

---

## Создание бэкапа

### Одной командой

```bash
D=/root/backup_$(date +%Y%m%d_%H%M)_manual
mkdir -p $D

tar czf $D/rag_project.tar.gz \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -C /opt rag_project

tar czf $D/configs.tar.gz \
    /etc/nginx/sites-available/rag.conf \
    /etc/systemd/system/streamlit.service \
    /etc/systemd/network/10-eth0.network \
    /etc/letsencrypt 2>/dev/null

docker run --rm \
    -v rag_project_qdrant_data:/qdrant/storage:ro \
    -v $D:/backup \
    alpine tar czf /backup/qdrant_storage.tar.gz -C /qdrant storage

cd $D && tar czf rag_full_backup.tar.gz *
ls -lh rag_full_backup.tar.gz
echo "Путь: $D"
Что получится
text
/root/backup_20261011_1905/
├── rag_project.tar.gz         ~5 МБ
├── configs.tar.gz             ~10 КБ
├── qdrant_storage.tar.gz      ~90 МБ
└── rag_full_backup.tar.gz     ~95 МБ (всё вместе)
Скачать на комп
powershell
scp root@45.87.219.76:/root/backup_*/rag_full_backup.tar.gz D:\backup_rag\
Автоматизация через cron
Ежедневный бэкап в 3:00
bash
mkdir -p /opt/rag_project/scripts

cat > /opt/rag_project/scripts/backup.sh <<'EOF'
#!/bin/bash
set -e

D=/root/backup_$(date +%Y%m%d_%H%M)
mkdir -p $D

# Код + БД + документы
tar czf $D/rag_project.tar.gz \
    --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
    -C /opt rag_project

# Конфиги
tar czf $D/configs.tar.gz \
    /etc/nginx/sites-available/rag.conf \
    /etc/systemd/system/streamlit.service \
    /etc/systemd/network/10-eth0.network \
    /etc/letsencrypt 2>/dev/null

# Qdrant
docker run --rm \
    -v rag_project_qdrant_data:/qdrant/storage:ro \
    -v $D:/backup \
    alpine tar czf /backup/qdrant_storage.tar.gz -C /qdrant storage

# Общий архив
cd $D && tar czf rag_full_backup.tar.gz *

# Ротация — храним последние 7 бэкапов
cd /root
ls -1dt backup_* | tail -n +8 | xargs -r rm -rf

echo "Бэкап создан: $D/rag_full_backup.tar.gz"
EOF

chmod +x /opt/rag_project/scripts/backup.sh
Проверка:

bash
/opt/rag_project/scripts/backup.sh
Cron:

bash
crontab -e
Добавь строку:

text
0 3 * * * /opt/rag_project/scripts/backup.sh >> /var/log/rag_backup.log 2>&1
Восстановление
Из архива на сервере
bash
cd /root
ls -la backup_*/rag_full_backup.tar.gz    # найди нужный бэкап
D=/root/backup_20261011_1905              # подставь нужный

mkdir -p /tmp/restore && cd /tmp/restore
tar xzf $D/rag_full_backup.tar.gz

# 1. Код
systemctl stop streamlit
tar xzf rag_project.tar.gz -C /opt
# ВАЖНО: .env и history.db внутри архива
# Если восстанавливаешь на другой сервер — правь .env

# 2. Конфиги
tar xzf configs.tar.gz -C / 2>/dev/null

# 3. Qdrant
docker run --rm \
    -v rag_project_qdrant_data:/qdrant/storage \
    -v /tmp/restore:/backup \
    alpine tar xzf /backup/qdrant_storage.tar.gz -C /qdrant

# 4. Перезапуск
systemctl daemon-reload
systemctl restart streamlit

# 5. Проверка
systemctl status streamlit --no-pager | head -5
curl -I https://ai.ceny24.by
Полное восстановление VPS с нуля
Разверни новый VPS (см. deployment.md)

Скопируй бэкап на новый сервер:

powershell
scp D:\backup_rag\rag_full_backup.tar.gz root@NEW_IP:/root/
Восстанови по инструкции выше

Проверь .env — если менял SECRET_KEY, все сессии сбросятся

Проверь nginx конфиг — если домен другой, поправь server_name

Перевыпусти SSL:

bash
certbot --nginx -d ai.новыйдомен.by -d admin.новыйдомен.by \
    --agree-tos --register-unsafely-without-email --redirect
Что важно помнить
🔑 .env — самый важный файл
В нём:

ADMIN_PASSWORD_HASH — иначе не зайти в админку

SECRET_KEY — если потерять, все пользователи разлогинятся

Храни копию .env отдельно (не только внутри архива).

📁 history.db — SQLite с пользователями
Внутри:

users — логины, хэши паролей, роли

queries — история запросов

Не удаляй при восстановлении.

📄 user_documents/ — все документы пользователей
Внутри: user_documents/{user_id}/файлы.pdf

Обязательно сохраняй — иначе придётся перезагружать и переиндексировать всё.

🎯 Qdrant — векторная БД
Можно восстановить из бэкапа или переиндексировать всё заново. Второй способ занимает часы.

Проверка целостности бэкапа
SHA-256 хэш
После создания бэкапа:

bash
sha256sum /root/backup_*/rag_full_backup.tar.gz
После скачивания на комп:

powershell
Get-FileHash D:\backup_rag\rag_full_backup.tar.gz -Algorithm SHA256
Хэши должны совпадать. Если разные — бэкап побился при передаче.

Тестовое восстановление
Раз в месяц делай тестовое восстановление на отдельном сервере, чтобы убедиться, что бэкап рабочий.

Ротация бэкапов
Рекомендуемая схема:

Ежедневно — в 3:00, хранить последние 7

Еженедельно — каждое воскресенье, хранить последние 4

Ежемесячно — 1-е число, хранить 12

Где хранить:

На сервере — /root/backup_*/

На компе — D:\backup_rag\ (обязательно!)

В облаке — Яндекс.Диск / Google Drive (опционально)

Аварийные ситуации
Сервер не отвечает
Через панель HostFly → Перезагрузка

Если не помогло → noVNC Console → вход → systemctl restart streamlit

Крайний случай → Переустановка VPS + восстановление из бэкапа

Qdrant упал
bash
docker restart qdrant
docker logs qdrant | tail -50
Если не поднимается — восстанови из qdrant_storage.tar.gz.

Streamlit упал
bash
systemctl restart streamlit
journalctl -u streamlit -n 100 --no-pager
Модель в Ollama удалена
bash
docker exec ollama ollama pull qwen2.5:14b
# 10–15 минут
Забыт пароль админа
bash
cd /opt/rag_project
source venv/bin/activate

python3 <<'PY'
import sqlite3, hashlib
h = hashlib.sha256(b'НовыйПароль123').hexdigest()
conn = sqlite3.connect('history.db')
c = conn.cursor()
c.execute('UPDATE users SET password_hash=? WHERE username=?', (h, 'admin'))
conn.commit()
print('Пароль админа изменён')
conn.close()
PY

systemctl restart streamlit
Чек-лист бэкапа
□ Код проекта (rag_project.tar.gz)
□ .env (отдельно!)
□ history.db (внутри архива)
□ user_documents/ (внутри архива)
□ Qdrant volume (qdrant_storage.tar.gz)
□ Конфиги Nginx
□ systemd-сервисы
□ Let's Encrypt сертификаты
□ Хэш SHA-256 проверен
□ Копия на компе (D:\backup_rag\)
□ Автобэкап в cron настроен
□ Тестовое восстановление сделано (раз в месяц)
Команды-шпаргалка
bash
# Создать бэкап
/opt/rag_project/scripts/backup.sh

# Список бэкапов
ls -lh /root/backup_*/

# Проверить размер
du -sh /root/backup_*/

# Удалить старый
rm -rf /root/backup_20260901_0300

# Восстановить код
tar xzf /root/backup_XXX/rag_project.tar.gz -C /opt

# Восстановить Qdrant
docker run --rm -v rag_project_qdrant_data:/qdrant/storage \
    -v /root/backup_XXX:/backup alpine \
    tar xzf /backup/qdrant_storage.tar.gz -C /qdrant

# Перезапуск после восстановления
systemctl restart streamlit
Заключение
Правило номер один: перед любыми изменениями — бэкап.

bash
/opt/rag_project/scripts/backup.sh
Правило номер два: бэкап на сервере — не бэкап. Скачивай на комп регулярно.

Правило номер три: раз в месяц — тестовое восстановление на отдельном сервере.
