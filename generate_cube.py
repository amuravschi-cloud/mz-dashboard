#!/usr/bin/env python3
"""
Скрипт пересборки OLAP-куба валовой прибыли Metalica Zuev и шифрования для веб-дашборда.

Входные данные:
1. --sales: Excel-выгрузка продаж из 1С («для вал приб 1с бух_коды.xlsx»)
2. --nom-spr: Excel-справочник номенклатуры 1С («PBI_SprNomNew.xlsx»)
3. --clients-json: JSON-справочник привязки клиентов к менеджерам и группам доступа («managers_clients.json»)

Выходные данные:
1. --out-cube: dashboard_cube.json (локальный компактный JSON куба)
2. --out-enc: data.enc.js (зашифрованный AES-256-GCM + PBKDF2 payload для веб-интерфейса)
"""

import argparse
import base64
import json
import os
import re
import sys
import warnings

import pandas as pd
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

warnings.filterwarnings('ignore')

MONTH_NAMES_RU = {
    1: 'Янв', 2: 'Фев', 3: 'Мар', 4: 'Апр', 5: 'Май', 6: 'Июн',
    7: 'Июл', 8: 'Авг', 9: 'Сен', 10: 'Окт', 11: 'Ноя', 12: 'Дек'
}

GROUPS_ORDER = [
    'Арматура', 'Трубы профильные', 'Лист', 'Балка', 'Швеллер',
    'Круг', 'Уголок', 'Трубы круглые', 'Прочее', 'Полоса', 'Квадрат'
]


def map_dashboard_group(chain):
    if not chain:
        return 'Прочее', 'Прочее'
    l2 = chain[1] if len(chain) > 1 else chain[0]
    l3 = chain[2] if len(chain) > 2 else ''
    if l2 == 'BARE DIN OTEL':
        if 'BETON' in l3:
            return 'Арматура', l3
        elif 'ROTUND' in l3:
            return 'Круг', l3
        elif 'LAT' in l3:
            return 'Полоса', l3
        elif 'PATRAT' in l3:
            return 'Квадрат', l3
        else:
            return 'Арматура', l3
    elif l2 == 'PROFILE':
        if 'PROFIL L' in l3 or 'CORNIER' in l3:
            return 'Уголок', l3
        elif 'PROFIL U' in l3 or 'UPN' in l3:
            return 'Швеллер', l3
        elif 'PROFIL I' in l3 or 'HEA' in l3 or 'HEB' in l3:
            return 'Балка', l3
        else:
            return 'Профили', l3
    elif l2 == 'TEAVA PROFILATA':
        return 'Трубы профильные', l3
    elif l2 == 'TEAVA ROTUNDA':
        return 'Трубы круглые', l3
    elif l2 in ['TABLA LAMINATA', 'TABLA ZINCATA IN VAL']:
        return 'Лист', l3
    else:
        return 'Прочее', l2


def extract_retail_mgr(name):
    m = re.search(r'\((.+?)\)', str(name))
    if m:
        return m.group(1).strip()
    m2 = re.search(r'[Рр]озничный покупатель\s+(.+)', str(name))
    if m2:
        return m2.group(1).strip()
    return 'Розница (общая)'


def main():
    parser = argparse.ArgumentParser(description="Сборка OLAP-куба для дашборда Metalica Zuev")
    parser.add_argument('--sales', default="/Users/user/Documents/MZ Аналитика/для вал приб 1с бух_коды.xlsx",
                        help="Путь к файлу продаж (Excel)")
    parser.add_argument('--nom-spr', default="/Users/user/Documents/MZ Аналитика/tmp/PBI_SprNomNew.xlsx",
                        help="Путь к справочнику номенклатуры (Excel)")
    parser.add_argument('--clients-json', default="/Users/user/Documents/mz_refs_data/managers_clients.json",
                        help="Путь к справочнику клиентов и менеджеров (JSON)")
    parser.add_argument('--out-cube', default="dashboard_cube.json",
                        help="Выходной файл JSON куба")
    parser.add_argument('--out-enc', default="data.enc.js",
                        help="Выходной файл зашифрованного JS payload")
    parser.add_argument('--password', default=os.environ.get("MZ_PASSWORD", "MZ2026"),
                        help="Пароль шифрования (по умолчанию MZ2026)")
    args = parser.parse_args()

    print(f"Загрузка продаж: {args.sales}")
    df = pd.read_excel(args.sales, sheet_name=0, header=7)
    df = df.rename(columns={
        'Контрагент': 'Контрагент',
        'Номенклатура': 'Номенклатура',
        'Склад': 'Склад',
        'Период': 'Период',
        'Номенклатура.Внешний код (Номенклатура)': 'Внешний_код',
        'Сумма без НДС': 'Сумма_без_НДС',
        'Количество': 'Количество',
        'Себестоимость': 'Себестоимость'
    })
    df['Контрагент'] = df['Контрагент'].ffill()
    df = df.dropna(subset=['Номенклатура'])
    df = df[df['Номенклатура'] != 'Номенклатура']
    df = df[df['Контрагент'] != 'Контрагент']
    df = df[df['Контрагент'].notna()]
    df['Контрагент'] = df['Контрагент'].astype(str).str.strip()
    df['Номенклатура'] = df['Номенклатура'].astype(str).str.strip()
    df['Склад'] = df['Склад'].astype(str).str.strip()

    for c in ['Сумма_без_НДС', 'Количество', 'Себестоимость']:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
    df['Период'] = pd.to_datetime(df['Период'], format='mixed', dayfirst=True, errors='coerce')
    df['Месяц'] = df['Период'].dt.month
    df['Внешний_код_clean'] = df['Внешний_код'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)

    print(f"Загрузка справочника номенклатуры: {args.nom-spr if hasattr(args, 'nom-spr') else args.nom_spr}")
    spr_path = args.nom_spr
    df_spr = pd.read_excel(spr_path, header=1)
    df_spr.columns = ['Номенклатура', 'B', 'C', 'D', 'В_группе', 'Это_группа', 'Код', 'Код_1С', 'Код_группы', 'Помечен_удален']
    df_spr = df_spr[df_spr['Номенклатура'].notna()]
    df_spr = df_spr[~df_spr['Номенклатура'].isin(['Параметры:', 'Номенклатура'])]

    groups = df_spr[df_spr['Это_группа'] == 'Да'][['Номенклатура', 'Код', 'Код_группы', 'В_группе']].copy()
    groups_dict = dict(zip(groups['Код'], groups['Номенклатура']))
    groups_parent = dict(zip(groups['Код'], groups['Код_группы']))

    def get_group_chain(code):
        chain = []
        cur = code
        for _ in range(10):
            if cur in groups_dict:
                chain.append(groups_dict[cur])
                cur = groups_parent.get(cur)
                if pd.isna(cur) or not cur:
                    break
            else:
                break
        return list(reversed(chain))

    items = df_spr[df_spr['Это_группа'] == 'Нет'].copy()
    items['Код_1С_clean'] = items['Код_1С'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    items['Цепочка'] = items['Код_группы'].apply(get_group_chain)
    code_chain_map = dict(zip(items['Код_1С_clean'], items['Цепочка']))

    df['Цепочка'] = df['Внешний_код_clean'].map(code_chain_map)
    df['Группа_Дашборд'], _ = zip(*df['Цепочка'].apply(map_dashboard_group))

    print(f"Загрузка справочника контрагентов: {args.clients_json}")
    with open(args.clients_json, 'r', encoding='utf-8') as f:
        mc = json.load(f)

    client_lookup = {str(c.get('name', '')).strip(): c for c in mc['clients']}

    def match_client(sc):
        clean = str(sc).strip()
        if clean in client_lookup:
            return client_lookup[clean]
        alt = clean.replace('"', '').replace('«', '').replace('»', '').strip()
        for k, v in client_lookup.items():
            if k.replace('"', '').replace('«', '').replace('»', '').strip() == alt:
                return v
        return {'manager': None, 'access_group': None}

    df['CI'] = df['Контрагент'].apply(match_client)
    df['Менеджер_1С'] = df['CI'].apply(lambda x: x.get('manager'))
    df['Группа_Доступа'] = df['CI'].apply(lambda x: x.get('access_group') or 'Другие/Без группы')

    retail_mask = df['Контрагент'].astype(str).str.contains('озничный покупатель', case=False, na=False)
    df['Канал'] = 'Опт'
    df.loc[retail_mask, 'Канал'] = 'Розница'

    df['Менеджер_Розница'] = df.apply(lambda r: extract_retail_mgr(r['Контрагент']) if r['Канал'] == 'Розница' else None, axis=1)
    df['Ответственный'] = df.apply(lambda r: r['Менеджер_Розница'] if r['Канал'] == 'Розница' else (r['Менеджер_1С'] or 'Без менеджера'), axis=1)
    df['Ответственный'] = df['Ответственный'].fillna('Без менеджера')

    agg_cols = ['Месяц', 'Склад', 'Группа_Доступа', 'Канал', 'Группа_Дашборд', 'Номенклатура', 'Контрагент', 'Ответственный']
    grain = df.groupby(agg_cols).agg(
        rev=('Сумма_без_НДС', 'sum'),
        cost=('Себестоимость', 'sum'),
        tons=('Количество', 'sum'),
        tx=('Сумма_без_НДС', 'count')
    ).reset_index()

    warehouses = sorted([str(x) for x in df['Склад'].unique().tolist()])
    access_groups = sorted([str(x) for x in df['Группа_Доступа'].unique().tolist()])
    channels = ['Опт', 'Розница']
    items_list = sorted([str(x) for x in df['Номенклатура'].unique().tolist()])
    clients_list = sorted([str(x) for x in df['Контрагент'].unique().tolist()])
    managers_list = sorted([str(x) for x in df['Ответственный'].unique().tolist()])

    wh_map = {v: i for i, v in enumerate(warehouses)}
    ag_map = {v: i for i, v in enumerate(access_groups)}
    ch_map = {v: i for i, v in enumerate(channels)}
    grp_map = {v: i for i, v in enumerate(GROUPS_ORDER)}
    item_map = {v: i for i, v in enumerate(items_list)}
    client_map = {v: i for i, v in enumerate(clients_list)}
    mgr_map = {v: i for i, v in enumerate(managers_list)}

    records = []
    for _, r in grain.iterrows():
        records.append([
            int(r['Месяц']),
            wh_map[str(r['Склад'])],
            ag_map[str(r['Группа_Доступа'])],
            ch_map[str(r['Канал'])],
            grp_map.get(str(r['Группа_Дашборд']), grp_map['Прочее']),
            item_map[str(r['Номенклатура'])],
            client_map[str(r['Контрагент'])],
            mgr_map[str(r['Ответственный'])],
            round(float(r['rev']), 2),
            round(float(r['cost']), 2),
            round(float(r['tons']), 3),
            int(r['tx'])
        ])

    unique_months = sorted([int(m) for m in df['Месяц'].dropna().unique() if int(m) in MONTH_NAMES_RU])
    month_names = [MONTH_NAMES_RU[m] for m in unique_months]

    payload = {
        'months': unique_months,
        'month_names': month_names,
        'warehouses': warehouses,
        'access_groups': access_groups,
        'channels': channels,
        'groups': GROUPS_ORDER,
        'items': items_list,
        'clients': clients_list,
        'managers': managers_list,
        'records': records
    }

    dumped = json.dumps(payload, ensure_ascii=False)
    print(f"OLAP-куб собран: {len(records):,} записей, {len(dumped)/1024/1024:.2f} MB")

    if args.out_cube:
        with open(args.out_cube, 'w', encoding='utf-8') as f:
            f.write(dumped)
        print(f"Куб сохранён в {args.out_cube}")

    # Шифрование AES-256-GCM + PBKDF2
    plaintext = dumped.encode('utf-8')
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    key = kdf.derive(args.password.encode('utf-8'))
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    packed = salt + nonce + ciphertext
    encoded = base64.b64encode(packed).decode('ascii')

    with open(args.out_enc, 'w', encoding='utf-8') as f:
        f.write(f'const ENCRYPTED_DATA = "{encoded}";\n')

    print(f"Зашифрованный файл записан в {args.out_enc}")
    print("Готово!")


if __name__ == '__main__':
    main()
