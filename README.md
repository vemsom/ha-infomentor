# InfoMentor för Home Assistant

> **English:** An unofficial Home Assistant integration for InfoMentor, the
> Swedish school platform. It signs in with BankID and exposes each child's
> school news, messages, calendar and after-school schedule as entities.
> Read-only: nothing is ever sent back to the school. Home Assistant 2026.10+.

Integrationen hämtar det skolan publicerar i InfoMentor och gör det till
entiteter i Home Assistant: nyheter, meddelanden, kalenderhändelser och — om du
slår på det — fritidsschemat. Varje barn får en egen enhet med barnets namn, så
att `calendar.<barn>_skola` och `sensor.<barn>_olasta_skolhandelser` hamnar under
samma enhet i gränssnittet.

Den är **läs-only**. Ingenting skickas tillbaka till InfoMentor: inga
närvaroregistreringar, inga svar, inga ändringar i kalendern. Den enda
skrivningen är en bieffekt av läsningen — när integrationen hämtar texten i ett
meddelande öppnas det och markeras som läst hos InfoMentor (se *Kända
begränsningar*).

## Krav

- Home Assistant **2026.10** eller senare.
- Ett InfoMentor-konto som vårdnadshavare, med minst ett länkat barn.
- BankID i telefonen.
- **Två skärmar vid inloggningen.** Home Assistant visar en QR-kod på en skärm
  och telefonen scannar den. En telefon kan inte scanna sin egen skärm.
- Inga extra Python-paket. `manifest.json` har `requirements: []` — QR-koden
  ritas av en medföljande ren Python-kodare, allt annat går på `aiohttp` och
  standardbiblioteket.

## Installation

### Manuellt

Kopiera mappen `infomentor` till Home Assistants `custom_components` och starta
om:

```sh
cp -r custom_components/infomentor /sökväg/till/config/custom_components/
```

Sökvägen är oftast `/config/custom_components/` om du kör Home Assistant OS
eller Supervised. Resultatet ska bli
`/config/custom_components/infomentor/manifest.json`.

### HACS (custom repository)

1. HACS → menyn uppe till höger → **Anpassade repon**.
2. Klistra in repots URL, välj typen **Integration**, lägg till.
3. Sök upp **InfoMentor** i HACS, installera.
4. Starta om Home Assistant.

Repot har `hacs.json` i roten och integrationen ligger i
`custom_components/infomentor/`, vilket är den struktur HACS förväntar sig.

## Konfiguration

1. *Inställningar → Enheter och tjänster → Lägg till integration → InfoMentor*.
2. Välj **BankID – skanna QR-kod på en annan enhet**.
3. Home Assistant visar en länk: *Öppna QR-koden i en webbläsare*. Öppna den på
   den enhet som ska visa koden — en surfplatta, en dator, en telefon du inte
   ska scanna med. Sidan innehåller bara QR-koden och en statusrad.
4. Scanna koden med BankID. Koden roteras automatiskt medan försöket pågår.
5. När BankID är godkänt går dialogen vidare av sig själv. Har kontot flera
   länkade barn får du välja vilka som ska få entiteter.
6. Klart. Integrationen sköter förnyelsen av sin egen session.

Länken till QR-sidan är en engångslänk: den slutar fungera så fort försöket är
klart, avbrutet eller har gått ut, och ger inte åtkomst till något annat. Sidan
bygger på Home Assistants konfigurerade URL och måste därför gå att nå från
enheten som visar koden — bakom en reverse proxy kräver det att den externa
URL:en är satt (se *Kända begränsningar*).

En config entry motsvarar **ett InfoMentor-konto**, det vill säga en
vårdnadshavare; två medvårdnadshavare med separata konton blir två poster.

### Alternativ

*Inställningar → Enheter och tjänster → InfoMentor → Konfigurera*:

- **Barn** — vilka av kontots länkade barn som ska ha entiteter.
- **Inkludera fritidsschemat** — avstängt som standard. Slås det på hämtas
  fritidsregistreringar sju dagar framåt och läggs in som händelser i barnets
  kalender. Det kostar en extra förfrågan per barn och poll.

Vill du hellre använda en befintlig refresh-token än BankID finns en avancerad
väg — och där beskrivs också hur inloggning, tokenförnyelse och hämtningen
fungerar: [docs/avancerat.md](docs/avancerat.md).

## Entiteter

Ett device per barn, med barnets namn som device-namn. `<barn>` i entitets-id:t
är barnets namn i gemener med understreck.

| Entitet | Typ | Värde | Attribut |
| --- | --- | --- | --- |
| `calendar.<barn>_skola` | kalender | Kalenderhändelser från idag och 30 dagar framåt. Med fritidsschemat påslaget även `Fritids 07:00–17:00` för de kommande sju dagarna. | Händelserna har `summary`, `description` och `uid`. Heldagshändelser blir datumspann. |
| `sensor.<barn>_olasta_skolhandelser` | sensor | Antal notifieringar som fortfarande är olästa (`New`). | `notifications`: de senaste tio posterna med `title`, `sent_at`, `type`, `subject` och `body`. |
| `sensor.<barn>_senaste_nyhet` | sensor | Den senaste nyhetens rubrik. | `published_at`, `news_id`, `title`, `content` (nyhetens hela text). |
| `sensor.<barn>_nasta_handelse` | sensor | Nästa kalenderhändelses rubrik. | `title`, `description`, `start`, `end`, `all_day`, `event_id`. |
| `sensor.<barn>_session` | sensor | `ok` eller `reauth_required`. | `last_update`, `last_update_success`, `include_childcare`, `notification` (orsaken när något är fel). |
| `sensor.infomentor_senaste_uppdatering` | sensor | Tidpunkten för senaste lyckade hämtning (`timestamp`). Hör till ett eget device för kontot, inte till ett barn. | `reauth_required`. |

Meddelandets ämne och text ligger i `notifications`-attributet på
`olasta_skolhandelser` — det finns ingen separat sensor för senaste meddelande.
Per hämtning öppnas det nyaste meddelandet plus eventuella olästa, högst tre,
för att inte belasta API:t i onödan. Ett meddelande som redan var läst när
funktionen infördes får ingen text förrän det är nyast igen.

Attributen är tänkta att användas i automationer och mallar; exempel på det
finns i [docs/exempel.md](docs/exempel.md).

## Exempel

Två färdiga exempel finns i [docs/exempel.md](docs/exempel.md): en blueprint
som skickar en notis när något nytt kommer från skolan, och en Lovelace-vy som
visar det senaste. Båda bygger enbart på entiteterna ovan och kan användas som
de är eller som utgångspunkt.

- [Blueprint för skolnotis](blueprints/automation/infomentor/skolnotis.yaml)
- [Lovelace-vyn skola](examples/lovelace/skola-vy.yaml)

## Kända begränsningar

- **Polling var 15:e minut, ingen push.** Nya meddelanden och ändringar syns
  inom en kvart. InfoMentors pushnotifikationer används inte.
- **Inloggningen behöver ibland göras om.** Hur länge en refresh-token lever är
  inte känt i detalj. När InfoMentor avvisar den startar Home Assistant ett
  reauth-flöde i stället för att tyst sluta uppdatera — `session`-sensorn visar
  `reauth_required` och orsaken finns i attributet `notification`.
- **Att läsa är att läsa.** När texten i ett meddelande hämtas öppnas det hos
  InfoMentor och markeras som läst. Oläst-räknaren i appen och på webben går
  alltså ner när integrationen har hämtat texten.
- **BankID på samma telefon fungerar inte.** Menyn har alternativet *BankID – på
  den här telefonen*, men inloggningen fullföljs inte den vägen. Använd QR-koden
  på en annan skärm.
- **BankID-inloggningen är inte helt pålitlig än.** Du kan starta en order och
  godkänna i BankID, men inloggningen kan stanna efter godkännandet i stället
  för att ge en session. Reservvägen är att klistra in en refresh-token.
- **QR-koden kräver en nåbar URL.** Sidan måste gå att öppna från enheten som
  visar koden, vilket bakom en reverse proxy kan kräva att den externa URL:en är
  konfigurerad i Home Assistant.
- **Inga bilagor.** Nyhetsbilagor (PDF, DOCX) hämtas inte; bara rubrik, text och
  publiceringsdatum.
- **Ett konto per config entry.** Två vårdnadshavare med separata konton ger två
  poster. Samma barnuppsättning kan bara läggas upp en gång.
- **Kalenderfönstret är idag till 30 dagar framåt.** Ingen historik, eftersom
  varje hämtning annars blir dyrare än den är värd.
- **Heldagshändelser** blir heldagsposter i Home Assistant; InfoMentors exakta
  sluttid för en heldag är inte modellerad.
- **Inloggningen går via skolans Stockholms-SSO** (`stockholm_par`), samma väg
  som mobilappen använder. Andra kommuners uppsättning är inte provad.

## Felsökning

**Sessionen har gått ut.** `sensor.<barn>_session` visar `reauth_required`.
Öppna integrationen och kör om autentiseringen — BankID-vägen används
automatiskt. Attributet `notification` säger varför. Ett BankID som tillhör ett
annat konto avvisas, så en felaktig inloggning kan inte ersätta en fungerande
post.

**Inga entiteter alls.** Kontrollera att posten är inlagd och att minst ett barn
är valt under *Konfigurera*. Har du flera barn och valde bort alla blir ingenting
skapat. Efter en ändring av inställningarna laddas posten om automatiskt.

**Entiteterna blir oattribuerade (`unavailable`).** Det betyder att hämtningen
misslyckades, oftast ett nätverksfel mot InfoMentor. Sessionen står kvar och
visar `ok`, och `sensor.infomentor_senaste_uppdatering` visar när det senast
lyckades. Nästa poll brukar rätta till det. Fortsätter det: titta i Home
Assistants logg efter `infomentor` — felmeddelandena är avsiktligt
innehållslösa (inga tokens, inga personuppgifter) men skiljer på nätverksfel,
HTTP-status och oväntat svarsformat.

**`cannot_connect` i inloggningsdialogen.** Nätverksfel mot InfoMentor, inte fel
lösenord eller fel konto. Prova igen.

**QR-sidan säger att den är stängd, eller ger 404.** Försöket är slut. Gå
tillbaka till Home Assistant, som visar nästa steg eller en ny kod.

## Attribution

Integrationen är byggd ovanpå det öppna projektet
[`infomentor-mcp`](https://github.com/wicol/infomentor-mcp) (MIT), som visat hur
BankID- och app-auktoriseringen fungerar, och använder InfoMentors egna
endpoints — samma som mobilappen anropar. Protokollet är portat till `aiohttp`
och omskrivet för Home Assistants config flow; ingenting från det projektet
behövs vid körning.

QR-kodaren i `custom_components/infomentor/qrcodegen.py` är
[QR Code generator library](https://www.nayuki.io/page/qr-code-generator-library)
av Project Nayuki (MIT), och medföljer oförändrad bortsett från en lint-rad i
filhuvudet. Den finns med för att integrationen ska kunna rita QR-koden utan
runtime-beroenden.

Det här är en **inofficiell integration**. Den har ingen koppling till
InfoMentor och används på eget ansvar; varumärket InfoMentor tillhör InfoMentor.
API:erna är inte publika och kan ändras utan förvarning.

## Utveckling

```sh
uv venv .venv-test
uv pip install --python .venv-test/bin/python pytest-homeassistant-custom-component aiohttp ruff
.venv-test/bin/python -m pytest -q
.venv-test/bin/ruff check custom_components tests --line-length 100
```

Testerna körs mot en Home Assistant-core i testläge med en lokal fejkserver i
stället för InfoMentor. Se `tests/` för vad sviten täcker.

## Licens

MIT, se [LICENSE](LICENSE).
