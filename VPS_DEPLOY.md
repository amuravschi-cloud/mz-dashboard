# Инструкция для ИИ-агента / Разработчика по развертыванию на VPS

Этот документ предназначен для агента, который будет переносить дашборд валовой прибыли (`mz-dashboard`) на рабочий VPS компании Metalica Zuev и подключать его к общему пулу дашбордов.

---

## 1. Общая картина инфраструктуры

| Параметр | Значение |
|---|---|
| **Рабочий VPS** | Каталог пула дашбордов: `/opt/dashboard/` |
| **Веб-сервер пула** | **Caddy** (отдаёт `/debt/`, `/clients/`, `/warehouse/`, `/pult/`, `/transport/` и др.) |
| **Папка автовыгрузок 1С по FTP на VPS** | `/opt/dashboard/incoming/` |
| **Папка справочников контрагентов на VPS** | `/opt/dashboard/dz/raw/` |
| **GitHub репозиторий дашборда** | `https://github.com/amuravschi-cloud/mz-dashboard.git` |
| **Пароль к дашборду** | `MZ2026` |

---

## 2. Точная карта расположения файлов

### А. Где лежат файлы на Mac разработчика:
1. **Выгрузка продаж 1С (факт валовой прибыли с кодами номенклатуры):**  
   `/Users/user/Documents/MZ Аналитика/для вал приб 1с бух_коды.xlsx`
2. **Справочник дерева групп номенклатуры 1С:**  
   `/Users/user/Documents/MZ Аналитика/tmp/PBI_SprNomNew.xlsx`
3. **Справочник привязки клиентов к менеджерам и группам доступа:**  
   `/Users/user/Documents/mz_refs_data/managers_clients.json`
4. **Сырые карточки 1С (клиенты, сотрудники, группы):**  
   - Контрагенты: `/Users/user/Documents/MZ Аналитика/dashboard/dz/raw/customers.json`
   - Менеджеры: `/Users/user/Documents/MZ Аналитика/dashboard/dz/raw/employees.json`
   - Группы доступа: `/Users/user/Documents/MZ Аналитика/dashboard/dz/raw/access_groups_ref.json`
5. **Собранный нешифрованный OLAP-куб (JSON, 36 000 строк):**  
   `/Users/user/Documents/MZ Аналитика/dashboard_cube.json`
6. **Папка веб-дашборда (Git репо):**  
   `/Users/user/Documents/MZ Аналитика/mz-dashboard/`

---

### Б. Где эти же файлы лежат (или куда их положить) на VPS:
1. **Справочник номенклатуры уже есть на VPS:**  
   `/opt/dashboard/incoming/PBI_SprNomNew.xlsx`
2. **Справочники контрагентов и менеджеров уже есть на VPS:**  
   - `/opt/dashboard/dz/raw/customers.json`
   - `/opt/dashboard/dz/raw/employees.json`
   - `/opt/dashboard/dz/raw/access_groups_ref.json`
3. **Файл `managers_clients.json` (агрегированная привязка):**  
   Скопировать с Mac или сгенерировать из OData/dz raw в `/opt/dashboard/refs/managers_clients.json`.
4. **Файл продаж `для вал приб 1с бух_коды.xlsx`:**  
   Скопировать в `/opt/dashboard/incoming/для вал приб 1с бух_коды.xlsx` (или `AI_ValovaiaPribyl.xlsx`).
5. **Целевая папка нового дашборда на VPS:**  
   `/opt/dashboard/profit_cube/` (или `/opt/dashboard/public/profit/`)

---

## 3. Пошаговый сценарий установки на VPS

### Шаг 1. Клонирование репозитория на VPS
```bash
cd /opt/dashboard
git clone https://github.com/amuravschi-cloud/mz-dashboard.git profit_cube
cd profit_cube
```

### Шаг 2. Установка Python-зависимостей (в venv пула дашбордов)
```bash
pip install -r requirements.txt
# Или в системный venv дашборда:
# /opt/dashboard/.venv/bin/pip install -r requirements.txt
```

### Шаг 3. Запуск генератора куба на VPS
Скрипт `generate_cube.py` читает исходники и генерирует зашифрованный `data.enc.js`:
```bash
python3 generate_cube.py \
  --sales "/opt/dashboard/incoming/для вал приб 1с бух_коды.xlsx" \
  --nom-spr "/opt/dashboard/incoming/PBI_SprNomNew.xlsx" \
  --clients-json "/opt/dashboard/refs/managers_clients.json" \
  --out-cube "/opt/dashboard/profit_cube/dashboard_cube.json" \
  --out-enc "/opt/dashboard/profit_cube/data.enc.js" \
  --password "MZ2026"
```

---

## 4. Подключение к Caddy (пул дашбордов)

В конфигурационном файле Caddy (`/etc/caddy/Caddyfile` или в конфигурации пула) добавьте секцию для нового дашборда:

```caddy
# Вариант: путь /profit-cube/* или отдельный поддомен
handle_path /profit-cube/* {
    root * /opt/dashboard/profit_cube
    file_server
}

# Или если отдаётся как корень отдельного виртуального хоста:
profit.mz.md {
    root * /opt/dashboard/profit_cube
    file_server
    encode gzip zstd
}
```

После редактирования перезагрузить Caddy:
```bash
systemctl reload caddy
```

---

## 5. Автоматическое обновление данных (Cron)

Когда 1С выгружает свежий файл продаж, для автоматического обновления данных достаточно добавить в cron команду:
```bash
0 8 * * * /opt/dashboard/.venv/bin/python3 /opt/dashboard/profit_cube/generate_cube.py \
  --sales "/opt/dashboard/incoming/для вал приб 1с бух_коды.xlsx" \
  --nom-spr "/opt/dashboard/incoming/PBI_SprNomNew.xlsx" \
  --clients-json "/opt/dashboard/refs/managers_clients.json" \
  --out-enc "/opt/dashboard/profit_cube/data.enc.js" \
  --password "MZ2026" >> /var/log/mz_cube_update.log 2>&1
```

---

## 6. Быстрая команда копирования недостающих файлов с Mac на VPS

Если на VPS ещё нет файла продаж или `managers_clients.json`, их можно перенести с Mac одной строкой:

```bash
# Выполнить на Mac:
rsync -avz \
  "/Users/user/Documents/MZ Аналитика/для вал приб 1с бух_коды.xlsx" \
  "/Users/user/Documents/MZ Аналитика/tmp/PBI_SprNomNew.xlsx" \
  "/Users/user/Documents/mz_refs_data/managers_clients.json" \
  user@169.58.176.231:/opt/dashboard/incoming/
```
