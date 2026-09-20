# OLX Monitor Bot — Instrukcja obsługi

**OLX Monitor Bot** to program, który automatycznie monitoruje wybrane wyszukiwania na OLX
i wysyła Ci powiadomienia na Telegram, gdy tylko pojawi się nowe ogłoszenie.

Działa na komputerze (Windows, macOS, Linux), jest prosty w obsłudze i nie wymaga
wiedzy programistycznej.

## Co potrafi

- Skanuje OLX automatycznie co losowy czas (domyślnie co 150–300 sekund),
- omija zabezpieczenia Cloudflare (udaje przeglądarkę Chrome),
- wysyła na Telegram ładnie sformatowane wiadomości: **tytuł, cena, lokalizacja, link** oraz przycisk prowadzący wprost do oferty,
- nie wysyła duplikatów (pamięta, które oferty już widział),
- obsługuje wiele adresów URL jednocześnie,
- ma opcjonalny filtr słów kluczowych w tytule,
- posiada panel sterowania w przeglądarce (ciemny motyw).

## Zawartość pakietu

| Plik | Opis |
|---|---|
| `app.py` | Główny program (backend + panel www) |
| `requirements.txt` | Lista wymaganych bibliotek Pythona |
| `run.bat` | Skrypt startowy dla **Windows** (kliknij dwukrotnie) |
| `run.sh` | Skrypt startowy dla **macOS / Linux** |
| `README.md` | Ta instrukcja |

Pliki tworzone automatycznie podczas działania:

| Plik | Opis |
|---|---|
| `config.json` | Twoja konfiguracja (token, Chat ID, adresy URL, interwały) |
| `seen_offers.json` | Historia widzianych ofert (ochrona przed duplikatami) |
| `bot.log` | Dziennik zdarzeń programu |
| `venv/` | Środowisko wirtualne Pythona (tworzone przy pierwszym uruchomieniu) |

---

## Krok 1 — Wymagania wstępne (instalacja Pythona)

Program wymaga zainstalowanego **Python 3.10 lub nowszego**.

### Windows

1. Wejdź na stronę https://www.python.org/downloads/ i pobierz najnowszy instalator.
2. Uruchom instalator i **koniecznie zaznacz opcję „Add python.exe to PATH"** na pierwszym ekranie.
3. Kliknij „Install Now" i poczekaj na zakończenie instalacji.
4. Sprawdzenie: otwórz „Wiersz polecenia" (naciśnij `Win + R`, wpisz `cmd`, Enter)
   i wpisz `python --version`. Powinieneś zobaczyć np. `Python 3.12.4`.

### macOS

- Opcja A: pobierz instalator ze strony https://www.python.org/downloads/ (najprostsza),
- Opcja B: przez Homebrew: `brew install python`.

Sprawdzenie: otwórz Terminal i wpisz `python3 --version`.

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

---

## Krok 2 — Tworzenie bota na Telegramie

Powiadomienia są wysyłane przez **Twojego własnego bota** na Telegramie. Potrzebujesz dwóch rzeczy:
**Token bota** i **Chat ID** (Twój identyfikator czatu).

### 2.1. Skąd wziąć Token bota

1. Otwórz Telegram (aplikacja na telefonie lub komputerze).
2. W polu wyszukiwania wpisz **@BotFather** — wybierz konto z niebieskim znaczkiem weryfikacji ✔.
3. Wyślij do niego komendę `/start`, a następnie `/newbot`.
4. BotFather poprosi o **nazwę** bota (np. „Mój Monitor OLX") — może być dowolna.
5. Następnie poprosi o **nazwę użytkownika** (username) — musi kończyć się słowem `bot`,
   np. `moj_monitor_olx_bot`.
6. Po chwili BotFather wyśle wiadomość z **Tokenem** — wygląda mniej więcej tak:

   ```
   7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

7. **Skopiuj i zapisz ten token w bezpiecznym miejscu** — to „hasło" Twojego bota.
   Nie udostępniaj go nikomu.

### 2.2. Skąd wziąć Chat ID

**Sposób A (najprostszy):**

1. W Telegramie wyszukaj **@userinfobot** (również konto zweryfikowane).
2. Wyślij mu `/start`.
3. Bot odpowie wiadomością zawierającą Twoje **Id** (np. `Id: 123456789`) —
   to jest Twój Chat ID.

**Sposób B (przez przeglądarkę):**

1. Otwórz w Telegramie czat ze swoim nowo utworzonym botem i wyślij mu komendę `/start`
   (albo dowolną wiadomość — to **konieczne**, inaczej bot nie będzie mógł do Ciebie pisać).
2. Wklej w przeglądarce adres:

   ```
   https://api.telegram.org/bot<TUTAJ_WKLEJ_TOKEN>/getUpdates
   ```

   (zamiast `<TUTAJ_WKLEJ_TOKEN>` wstaw swój token, bez nawiasów)
3. Na stronie znajdź fragment `"chat":{"id":123456789` — liczba to Twój Chat ID.

> **Ważne:** zanim zaczniesz używać programu, wyślij swojemu botowi `/start`
> (kliknij przycisk „Start" w czacie z botem). Bez tego bot nie może wysłać Ci wiadomości.

---

## Krok 3 — Pierwsze uruchomienie

### Windows

Kliknij dwukrotnie plik **`run.bat`** w folderze programu.

### macOS / Linux

Otwórz Terminal w folderze programu i wpisz:

```bash
./run.sh
```

Jeśli pojawi się błąd „Permission denied", najpierw wykonaj:
`chmod +x run.sh`, a potem ponownie `./run.sh`.

### Co się dzieje przy pierwszym uruchomieniu

1. Skrypt tworzy środowisko wirtualne `venv/` (może potrwać ~1 minutę),
2. instaluje wymagane biblioteki (potrzebne połączenie z internetem),
3. uruchamia program — zobaczysz okno z komunikatem:

   ```
   ============================================================
     OLX Monitor Bot
     Panel administracyjny:  http://127.0.0.1:5000
   ============================================================
   ```

4. Otwórz przeglądarkę i wejdź na: **http://127.0.0.1:5000**

### Pierwsza konfiguracja w panelu

1. Wklej **Token bota** i **Chat ID** (z Kroku 2).
2. Wklej **adresy URL z OLX** — jeden na linię (patrz Krok 4).
3. Kliknij **„Zapisz konfigurację"**.
4. Kliknij **„Test wiadomości Telegram"** — na Telegramie powinna pojawić się wiadomość testowa.
5. Kliknij **„Skanuj teraz"**, aby od razu sprawdzić swoje wyszukiwania.

> **Uwaga:** okno programu musi pozostać otwarte — zamknięcie okna zatrzymuje monitorowanie.
> Program działa wyłącznie lokalnie (adres 127.0.0.1), panelu nie widać z internetu.

---

## Krok 4 — Ustawianie filtrów OLX (jak skopiować poprawny link)

Monitor korzysta z dokładnie tych samych filtrów, które ustawisz na stronie OLX.
Wystarczy skopiować adres z paska przeglądarki.

1. Wejdź na https://www.olx.pl
2. Wybierz **kategorię** i wpisz **szukaną frazę** (np. „iPhone 13").
3. Ustaw filtry według potrzeb, np.:
   - **Cena** — zakres od / do (np. 1000–3500 zł),
   - **Stan** — nowe / używane,
   - **Lokalizacja** — wybierz miasto lub „cała Polska", a następnie **promień w kilometrach**
     (np. 25 km) — suwak „odległość",
   - inne dostępne parametry.
4. Posortuj ogłoszenia: wybierz **„Najnowsze"** (sortowanie: `created_at:desc`).
   Dzięki temu najświeższe oferty trafiają na początek listy.
5. **Skopiuj cały adres URL z paska przeglądarki** (Ctrl+C / Cmd+C).
6. Wklej go w panelu w polu **„Adresy URL z OLX"**. Możesz wkleić kilka linków — każdy w osobnej linii.

Przykład prawidłowego linku (po ustawieniu filtrów ceny, odległości i sortowania):

```
https://www.olx.pl/elektronika/telefony/q-iphone-13/?search%5Bfilter_float_price%3Afrom%5D=1000&search%5Bfilter_float_price%3Ato%5D=3500&search%5Bdist%5D=25&search%5Border%5D=created_at%3Adesc
```

Co oznaczają ważne parametry w linku:

| Parametr | Znaczenie |
|---|---|
| `search[filter_float_price:from]` / `:to` | cena od / do |
| `search[dist]` | promień w km od wybranej lokalizacji |
| `search[order]=created_at:desc` | sortowanie od najnowszych (zalecane!) |
| `page` | numer strony (program ustawia go automatycznie) |

**Nie musisz nic zmieniać ręcznie w linku** — program skanuje dokładnie to,
co widzisz po jego otwarciu.

---

## Krok 5 — Najczęstsze pytania i rozwiązywanie problemów

### 1. Bot nie wysyła wiadomości

- Czy wysłałeś do bota `/start` w Telegramie? **To najczęstsza przyczyna.**
- Czy Token i Chat ID są poprawnie wklejone (bez spacji na końcach)?
- Czy w panelu zaznaczona jest opcja **„Wysyłaj powiadomienia Telegram"**?
- Kliknij **„Test wiadomości Telegram"** — jeśli test przechodzi, a powiadomień brak,
  sprawdź sekcję **„Dziennik zdarzeń"** w panelu (np. brak nowych ofert = poprawna praca).
- Sprawdź, czy nie zablokowałeś bota w Telegramie (wyślij mu `/start` ponownie).

### 2. Jak zmienić częstotliwość skanowania?

W panelu zmień pola **„Min. interwał skanowania"** i **„Maks. interwał skanowania"**
(np. 300 i 600) i kliknij „Zapisz konfigurację". Program losuje czas między tymi
wartościami, aby działał naturalnie. Nie ustawiaj wartości poniżej 30 sekund —
OLX może wtedy zablokować zapytania.

### 3. Błąd sieci / brak internetu

Jeśli komputer straci połączenie, w dzienniku zdarzeń pojawi się ostrzeżenie
„Nie udało się pobrać...", a program automatycznie spróbuje ponownie przy następnym
skanie. Po powrocie internetu wszystko wraca do normy — nie ma potrzeby restartu.

### 4. OLX zablokował zapytanie (Cloudflare / captcha)

Program udaje przeglądarkę Chrome, ale przy bardzo częstych zapytaniach OLX może
chwilowo blokować. Wtedy w panelu zobaczysz błąd „OLX zablokował zapytanie".
Rozwiązania:
- zwiększ interwały skanowania (np. 300–600 s),
- zmniejsz liczbę adresów URL,
- odczekaj kilkanaście minut — blokady zwykle są tymczasowe.

### 5. Strona panelu się nie otwiera / „Port 5000 zajęty"

Jeśli port 5000 jest zajęty przez inny program, aplikacja automatycznie spróbuje
kolejnych (5001, 5002...). Prawidłowy adres znajdziesz w oknie konsoli w linii
„Panel administracyjny". Sprawdź też, czy nie uruchomiłeś programu dwukrotnie.

### 6. Chcę zacząć od zera (wyczyścić historię)

Zatrzymaj program (Ctrl+C lub zamknij okno), usuń plik **`seen_offers.json`**
i uruchom ponownie. Program zapomni o wszystkich widzianych ofertach.
(Uwaga: jeśli opcja „Powiadamiaj o wszystkich ofertach z pierwszego skanu" jest
włączona, po restarcie możesz dostać dużo powiadomień naraz).

### 7. Program się nie uruchamia

- **Windows:** upewnij się, że Python jest zainstalowany **z zaznaczoną opcją PATH**
  (Krok 1). Sprawdź `python --version` w wierszu polecenia.
- **macOS/Linux:** sprawdź `python3 --version` w Terminalu.
- Sprawdź połączenie z internetem — pierwsze uruchomienie pobiera biblioteki.

### 8. Jak działają filtry słów kluczowych?

W polu **„Słowa kluczowe"** wpisz słowa oddzielone przecinkami (np. `iphone, pro`).
Tryby:
- **Tylko jeśli tytuł zawiera** — powiadomi tylko o ofertach, których tytuł zawiera
  którekolwiek ze słów,
- **Pomiń, jeśli tytuł zawiera** — pominie oferty zawierające którekolwiek ze słów
  (np. wpisz `uszkodzony, popękany`).

### 9. Co oznaczają poszczególne pola statusu?

- **Ostatni skan** — kiedy odbył się ostatni skan,
- **Następny skan za** — odliczanie do kolejnego skanu,
- **Zapamiętane oferty** — ile ofert program już widział (historia),
- **Wysłane powiadomienia** — łączna liczba wysłanych wiadomości Telegram.

---

## Bezpieczeństwo i uwagi prawne

- **Token bota to dane poufne** — nie udostępniaj go nikomu i nie wrzucaj do internetu.
- Panel działa tylko lokalnie (`127.0.0.1`) — nikt z zewnątrz nie ma do niego dostępu.
  Nie przekierowuj portów routera do panelu bez zabezpieczeń.
- Korzystaj z programu z rozsądną częstotliwością (min. 30–60 s między skanami),
  zgodnie z regulaminem serwisu OLX.
- Program przeznaczony jest do użytku własnego (monitorowanie interesujących Cię ofert).
