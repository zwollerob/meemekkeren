# Meezinger 🎤

**Zing mee met alles wat je pc afspeelt.** Meezinger is een kleine Windows-app die meeluistert
met je audio-uitvoer, het nummer herkent en de songtekst gelijk met de muziek laat meelopen,
als karaoke. Zo kun je tijdens het werk lekker meezingen.

- 🎧 **Luistert mee met elke bron**: Spotify, YouTube in de browser, radio of een lokaal bestand.
  Er wordt niets via je microfoon opgenomen.
- 🔎 **Herkent het nummer automatisch** via Shazam, inclusief de plek waar je in het nummer zit.
- 📝 **Haalt gesynchroniseerde songteksten op** van [LRCLIB](https://lrclib.net). Je hebt geen
  account of API-sleutel nodig.
- ⏱️ **Blijft in sync**: synchroniseert regelmatig opnieuw, pauzeert bij stilte en ziet het
  vanzelf als er een nieuw nummer begint.
- 🎬 **Ondertitelmodus**: alleen de tekst, op een transparante achtergrond die je kunt
  doorklikken en die altijd bovenop staat, zodat je gewoon verder kunt werken.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

## Hoe het werkt

1. **Meeluisteren**: via WASAPI-loopback (bibliotheek `soundcard`) neemt de app op wat je
   luidspreker of headset afspeelt. Er wordt niets via je microfoon opgenomen.
   In de stand **Automatisch** luistert de app naar *alle* uitvoerapparaten tegelijk en
   gebruikt het apparaat waar geluid uit komt. Speelt je browser via een ander apparaat dan
   het Windows-standaardapparaat, dan wordt dat dus ook opgepikt.
2. **Herkennen**: een fragment van 12 seconden gaat naar Shazam (via `shazamio`). Shazam
   geeft terug wélk nummer het is én waar in het nummer het fragment begon.
3. **Songtekst**: gesynchroniseerde teksten (LRC-formaat) worden opgehaald bij
   [LRCLIB](https://lrclib.net), een gratis database zonder API-sleutel.
4. **Synchroniseren**: een klok loopt vanaf de door Shazam gemelde positie. Elke 15 seconden
   wordt opnieuw herkend om drift, doorspoelen of een nieuw nummer op te vangen. Bij stilte
   (pauze) stopt de klok en daarna synchroniseert hij direct opnieuw.

## Installeren en starten

Je hebt **Python 3.10 of nieuwer** voor Windows nodig ([python.org](https://www.python.org/downloads/),
vink "Add python.exe to PATH" aan).

Dubbelklik op **`start.bat`**. De eerste keer installeert die de benodigde pakketten
(± 1 minuut), daarna start de app meteen.

Of handmatig:

```bash
py -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
.venv\Scripts\python meezinger.py
```

### Losse .exe maken

Dubbelklik op **`build_exe.bat`**. Daarna staat er `dist\Meezinger.exe` klaar die je zonder
Python kunt draaien en kunt delen.

## Bediening

| Toets / knop | Wat het doet |
|---|---|
| `←` / `−` | Tekst 0,25 s later laten lopen (tekst loopt voor) |
| `→` / `+` | Tekst 0,25 s eerder laten lopen (tekst loopt achter) |
| `0` / Reset | Handmatige sync-correctie terug op 0 |
| `R` / ⟳ Herken | Nu meteen opnieuw herkennen |
| `D` / 🔊-knop rechtsboven | Audio-apparaat kiezen (Automatisch of één vast apparaat) |
| `O` / 🎬 Ondertitelmodus | Alleen de tekst tonen, als ondertitels op een transparante achtergrond |
| `T` / 📌 Bovenop | Venster altijd bovenop houden (handig naast je werk) |

Het venster kun je zo klein of groot maken als je wilt, de tekst schaalt mee. Regels
verschijnen standaard 0,25 s vóór de zangeres of zanger inzet (`LYRICS_LEAD` bovenin
`meezinger.py`).

## Ondertitelmodus 🎬

Klik rechtsboven op **🎬 Ondertitelmodus** (of druk `O`). Het hoofdvenster gaat naar de
taakbalk en je krijgt alleen de songtekst te zien: de huidige regel in geel, met de volgende
regel(s) eronder, op een **transparante achtergrond**. Een zwarte rand houdt de tekst
leesbaar op elke achtergrond. Je kunt gewoon doorwerken: klikken op de transparante delen
gaat dwars door het ondertitelvenster heen naar het venster eronder.

- **Werkbalk**: beweeg je muis over de tekst. Je kunt dan:
  - ⠿ het venster verslepen (dat kan ook door de tekst zelf vast te pakken)
  - 📌 altijd bovenop aan- of uitzetten (staat standaard aan)
  - de tekst groter of kleiner maken met A− en A+
  - het venster smaller of breder maken met ⇔− en ⇔+
  - wisselen tussen 2 en 3 regels
  - ⟳ het nummer opnieuw laten herkennen
  - ✕ teruggaan naar het hoofdvenster
- **Rechtsklik** op de tekst geeft alle opties, ook de sync-correctie.
- **Dubbelklik** op de tekst brengt je terug naar het hoofdvenster.
- Positie, grootte en de andere ondertitelinstellingen worden onthouden.

## Hoort de app niets?

Rechtsboven zie je op welk apparaat de app luistert, met een **niveaumeter** eronder.
Groen met "hoort geluid" betekent dat er audio binnenkomt.

- Blijft de meter leeg terwijl je muziek hoort? Klik op de 🔊-knop. Het apparaat met
  **(speelt nu)** erachter is het apparaat waar het geluid uit komt. Kies dat, of laat
  de app op **Automatisch** staan.
- Kijk in de onderbalk of er een **⚠ audiofout** staat.
- Je keuze wordt onthouden in `%USERPROFILE%\.meezinger.json`.

## Goed om te weten

- **Herkennen duurt een paar tellen**: er is 12 s audio nodig, plus een paar seconden voor
  Shazam. Bij een nieuw nummer verschijnt de tekst dus na zo'n 12–15 seconden.
- **Niet elk nummer heeft een gesynchroniseerde tekst** op LRCLIB. Dan laat de app de gewone
  tekst zien (als die er is) die je zelf kunt scrollen.
- **Geluidsvolume**: de app luistert naar het digitale signaal, dus het werkt ook als je
  volume laag staat. Staat het volume in Windows op **gedempt**, dan komt er niets binnen.
- **Bluetooth-headsets** met veel vertraging: gebruik `+`/`−` om bij te stellen.
- **Live-versies, remixes en covers** worden soms als de studioversie herkend, waardoor de
  timing niet klopt.
- `shazamio` gebruikt een niet-officiële Shazam-API. Die werkt prima voor persoonlijk
  gebruik, maar kan in de toekomst veranderen. Dan helpt meestal
  `pip install -U shazamio`.

## Instellingen

Bovenin `meezinger.py` staan de belangrijkste instellingen, zoals `RETRY_LOCKED` (hoe vaak
er opnieuw gesynchroniseerd wordt), `SILENCE_RMS` (drempel voor stilte) en `LYRICS_LEAD`.
