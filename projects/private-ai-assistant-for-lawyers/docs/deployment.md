# 🚀 Развёртывание

Полная инструкция по развёртыванию RAG-ассистента на чистом VPS с нуля.
Время: ~2 часа на CPU-сервере.

## Требования

### Железо

| Компонент | Минимум | Рекомендуется |
|---|---|---|
| CPU | 8 ядер | 12+ ядер |
| RAM | 16 ГБ | 32 ГБ |
| Диск | 100 ГБ SSD | 200 ГБ SSD |
| GPU | — | RTX 3090 / A5000 |

### Софт
- ОС: Ubuntu 24.04 LTS
- Домен с A-записями
- Root-доступ

---

## Шаг 1. Настройка сети

Что нужно от провайдера: IP, маска, шлюз, DNS.

```bash
cat > /etc/systemd/network/10-eth0.network <<'EOF'
[Match]
Name=eth0

[Link]
MTUBytes=1400

[Network]
Address=45.87.219.76/25
Gateway=45.87.219.1
DNS=178.172.217.252
DNS=85.209.148.252
EOF

systemctl enable --now systemd-networkd
systemctl restart systemd-networkd
Проверка:

bash
ip -4 addr show eth0
ip route
ping -c 2 8.8.8.8
getent hosts pypi.org
Шаг 2. Базовая настройка
bash
apt update && apt upgrade -y
timedatectl set-timezone Europe/Minsk
apt install -y curl wget git nano htop net-tools
Шаг 3. Docker + Qdrant + Ollama
bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
Создай /opt/rag_project/docker-compose.yml:

yaml
version: '3.8'

services:
  qdrant:
    image: qdrant/qdrant:latest
    container_name: qdrant
    restart: always
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage

  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    restart: always
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama

volumes:
  qdrant_data:
    name: rag_project_qdrant_data
  ollama_data:
    name: rag_project_ollama_data
Запуск:

bash
cd /opt/rag_project
docker compose up -d
docker ps
Загрузка модели:

bash
docker exec ollama ollama pull qwen2.5:14b
docker exec ollama ollama list
Шаг 4. Python-зависимости
bash
apt install -y python3-pip python3-venv \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    poppler-utils build-essential

tesseract --version
tesseract --list-langs

cd /opt/rag_project
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
Шаг 5. Конфигурация приложения
bash
cd /opt/rag_project

cat > .env <<'EOF'
ADMIN_LOGIN=admin
ADMIN_PASSWORD_HASH=<хэш>
SECRET_KEY=<случайный ключ>
EOF

# Генерация хэша пароля
python3 -c "import hashlib; print(hashlib.sha256(b'МойПароль').hexdigest())"

# Генерация SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

mkdir -p user_documents static/icons avatars
Скопируй файлы проекта из src/ в /opt/rag_project/:

bash
cp /путь/к/репозиторию/src/*.py /opt/rag_project/
Первый запуск:

bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Шаг 6. Nginx + HTTPS
bash
apt install -y nginx certbot python3-certbot-nginx
DNS-записи у регистратора:

ai.твойдомен.by → IP

admin.твойдомен.by → IP

Конфиг /etc/nginx/sites-available/rag.conf (см. infra/nginx-rag.conf):

nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name ai.твойдомен.by;
    client_max_body_size 250M;
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
        proxy_buffering off;
    }
}

server {
    listen 80;
    server_name admin.твойдомен.by;
    client_max_body_size 250M;
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
        proxy_buffering off;
    }
}
Применяем:

bash
ln -sf /etc/nginx/sites-available/rag.conf /etc/nginx/sites-enabled/rag.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
HTTPS:

bash
certbot --nginx \
    -d ai.твойдомен.by \
    -d admin.твойдомен.by \
    --agree-tos \
    --register-unsafely-without-email \
    --redirect
Проверка:

bash
curl -I https://ai.твойдомен.by
curl -I https://admin.твойдомен.by
Шаг 7. Автозапуск через systemd
/etc/systemd/system/streamlit.service (см. infra/streamlit.service):

ini
[Unit]
Description=Streamlit RAG App
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/rag_project
Environment="PATH=/opt/rag_project/venv/bin"
ExecStart=/opt/rag_project/venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
Применяем:

bash
systemctl daemon-reload
systemctl enable --now streamlit
systemctl status streamlit
Шаг 8. Проверка автозапуска
bash
systemctl reboot
# Подожди 60 сек

ssh root@IP
systemctl status streamlit --no-pager | head -5
docker ps
curl -I http://localhost:8501
curl -I https://ai.твойдомен.by
Шаг 9. Первичная индексация
Зайди как admin на https://admin.твойдомен.by

Загрузи документы

Нажми «Индексировать»

Дождись ✅

Время индексации:

100 КБ — 1–2 мин

1 МБ — 15–30 мин

10 МБ — 2–5 часов на CPU

Чек-лист
□ Статический IP + DNS
□ Ubuntu обновлена
□ Docker работает
□ Qdrant запущен
□ Ollama с qwen2.5:14b
□ Python venv + зависимости
□ Tesseract OCR (ru, en)
□ .env с ключами
□ Nginx проксирует
□ Let's Encrypt выдан
□ Оба домена открываются
□ Автозапуск проверен
□ Первый документ проиндексирован
□ Тестовый запрос работает
Известные проблемы
Проблема	Решение
No module 'torch'	source venv/bin/activate
Connection refused Qdrant	docker compose up -d
Streamlit не стартует	lsof -i :8501
HTTPS не работает	nslookup, подождать 15 мин
OCR мусор	apt install tesseract-ocr-rus
Модель выдумывает	TEMPERATURE = 0.0 в app.py
