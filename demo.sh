#!/usr/bin/env bash
# Полный прогон продукта на сгенерированных данных. Ничего никуда не отправляется.
set -e

CLUB="Падел Фили"
DB="padel_return.db"
export PADEL_DRY_RUN=1          # каналы не дёргают внешние API, но стоимость считается
export PADEL_CHANNEL=whatsapp

rm -f "$DB" report.html
mkdir -p data/sample

echo "=== 1. Генерируем правдоподобную выгрузку клуба (1200 клиентов, 18 месяцев) ==="
python3 tools/gen_sample_data.py --clients 1200 --months 18 --out data/sample

echo
echo "=== 2. Создаём клуб ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" init --courts 4 --price-peak 4200 --price-offpeak 2800

echo
echo "=== 3. Импорт выгрузок + расчёт признаков и графа партнёрств ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" import \
    --clients data/sample/clients.csv --bookings data/sample/bookings.csv

echo
echo "=== 4. Сегментация спящих + гипотезы причин ухода ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" segment --name "Возврат, август"

echo
echo "=== 5. Планируем первое касание (ничего не отправляем) ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" plan --campaign 1 --limit 400 --channel whatsapp

echo
echo "=== 6. Вычитываем сообщения глазами (первые 5) ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" preview --campaign 1 -n 5

echo
echo "=== 7. Подтверждаем и отправляем ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" approve --campaign 1
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" run --campaign 1 --limit 400 \
    --channel whatsapp --at "$(python3 -c "from datetime import datetime,timedelta;print((datetime.now()+timedelta(days=9)).strftime('%Y-%m-%d 12:00'))")" \
    | tail -8

echo
echo "=== 8. Симулируем ответы клиентов, считаем атрибуцию, строим отчёт ==="
python3 tools/simulate.py --db "$DB" --club "$CLUB" --campaign 1 --out report.html

echo
echo "=== 9. Очередь подтверждений администратора (первые 3) ==="
python3 -m padelreturn.cli --db "$DB" --club "$CLUB" tasks --kind confirm_booking | head -22

echo
echo "Готово. Отчёт клубу: report.html"
