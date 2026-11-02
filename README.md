# SMC-Mixer → OP-Z: громкость треков фейдерами

Готовый профиль `CC / User` для M-VAVE SMC-Mixer, который превращает фейдеры
1–8 в громкость треков OP-Z, а Mute 1–8 — в mute треков.

**Прошивка не меняется.** Записывается только 6 282-байтный профиль настроек
в пользовательскую область flash. Заводская прошивка, `app.bin` и MD5 остаются
заводскими. Настройку можно откатить своим бэкапом.

Проверено на SMC-Mixer V012 и OP-Z. На V013 протокол настроек, скорее всего,
такой же, но отдельно не проверялся.

## Что получится

| Контрол | Сообщение |
|---|---|
| Фейдеры 1–8 | `CC16`, каналы 1–8 |
| Mute 1–8 | `CC53`, каналы 1–8, toggle 0/127 |
| Solo 1–8 | `CC20–27`, канал 1, toggle 0/127 |
| Энкодеры 1–8 | заводские `CC30–37`, канал 1 |

Фейдеры управляют громкостью треков OP-Z. Mute работает как обычный mute.

**Solo не делает настоящий solo:** в OP-Z нет отдельного solo-CC. Кнопки Solo
пока только отправляют `CC20–27`; для настоящего solo нужен MIDI-роутер или
кастомная прошивка.

## Быстрый старт (Linux)

Нужен `alsa-utils`:

```sh
# Arch / Omarchy
sudo pacman -S alsa-utils

# Debian / Ubuntu
sudo apt install alsa-utils
```

Подключите микшер к компьютеру, **не к OP-Z**.

```sh
git clone https://github.com/olegglzr-tech/smc-mixer-opz.git
cd smc-mixer-opz

./tools/smc_mixer.py ports
./tools/smc_mixer.py info

# Сначала свой бэкап
./tools/smc_mixer.py dump dumps/settings-mine.bin

# Записать готовый профиль
./tools/smc_mixer.py write profiles/opz-volume-faders.bin

# Проверить
./tools/smc_mixer.py show profiles/opz-volume-faders.bin --profile cc
```

Готовый профиль меняет только `CC / User`, DAW-профиль не трогает. Энкодеры в
нём заводские (`CC30–37`), поэтому он перезапишет текущие настройки энкодеров
в CC-профиле.

Альтернатива без готового профиля — изменить только нужные поля, не трогая
энкодеры:

```sh
./tools/smc_mixer.py apply-opz --out dumps/settings-opz.bin
```

## Режим микшера

После записи переключите live-режим:

- **User / CC:** зажмите `Shift` и нажмите крайнюю правую кнопку `Right`.
- **DAW:** зажмите `Shift` и нажмите крайнюю левую кнопку `Left`.

`Channel Left/Right` — это Bank Previous/Next, они не переключают режим.
После переподключения проверьте режим снова: в DAW фейдеры отправляют
Pitch Bend (`E0`), а не `CC16`.

Проверка на компьютере:

```sh
amidi -l
amidi -p hw:1,0,1 -d
```

Двигайте фейдер 1. В User/CC-режиме должно быть `B0 10 xx`. Если идёт
`E0 00 00 xx` — микшер в DAW-режиме.

## Настройка OP-Z

1. Подключите микшер к OP-Z по USB-C.
2. Включите приём внешнего MIDI: зажмите `Tempo + Screen` и, **не отпуская**,
   нажмите `2`.
3. Проверьте `channel_one_to_active`: если включено, канал 1 управляет активным
   треком, а не треком 1. Для схемы «фейдер 1 → трек 1» выключите опцию.
4. Проверьте, что MIDI-каналы треков OP-Z совпадают с каналами фейдеров.
5. Подвигайте фейдеры 1–8.

## Если не работает

- `E0` вместо `B0 10` — микшер в DAW-режиме, переключите `Shift + Right`.
- CC16 идёт, но OP-Z молчит — проверьте `incoming MIDI`,
  `channel_one_to_active` и MIDI-каналы треков.
- OP-Z не видит микшер или не хватает питания — попробуйте USB-C хаб с внешним
  питанием. OP-Z рассчитан примерно на 100 мА на USB-порт.
- Solo не солоит — так и задумано, см. выше.

## Ручная настройка (Windows/macOS)

В SMC-Mixer Tool / MidiSuite:

| Контрол | Тип | Канал | CC | Значения |
|---|---|---|---|---|
| Fader 1–8 | CC | 1–8 | 16 | min 0, max 127 |
| Mute 1–8 | CC Toggle | 1–8 | 53 | off 0, on 127 |
| Solo 1–8 | CC Toggle | 1 | 20–27 | off 0, on 127 |
| Knob 1–8 | CC Abs | 1 | 30–37 | min 0, max 127 |

После записи переключите live-режим в **User / CC**: `Shift + Right`.

## Откат

```sh
./tools/smc_mixer.py write dumps/settings-mine.bin
```

Это вернёт профиль, который был до настройки. Прошивка не меняется.

## Файлы

- `profiles/opz-volume-faders.bin` — готовый профиль настроек.
- `profiles/opz-volume-faders.json` — тот же профиль для SMC-Mixer-Tool.
- `docs/op-z-setup-ru.md` — подробная инструкция.
- `tools/smc_mixer.py` — утилита настройки через ALSA `amidi`.

SHA-256 профиля:
`a90f3984aea4bf9dcaa89a71032178386af475b109c9ea7f6e1ce7c658439070`.

Протокол настроек основан на
[deadjoe/SMC-Mixer-Tool](https://github.com/deadjoe/SMC-Mixer-Tool).
