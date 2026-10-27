# InfoMentor för Home Assistant

En liten, läsbar custom integration som visar barnets InfoMentor-data i Home
Assistant: skolnyheter, notifieringar, kalender och (valfritt) fritidsschema.

Integrationen är **inte** gjord för HACS. Den installeras genom att kopiera
katalogen — eller genom att klona repot — till `config/custom_components/` och
starta om Home Assistant.

Den är **read-only**: den läser skolnyheter, notifieringar, kalendern och
(eventuellt) fritidsregistreringar. Ingenting skrivs tillbaka till InfoMentor,
och ingen push-registrering görs.

Inloggningen sker med **BankID i config flowen**: Home Assistant visar en
roterande QR-kod, du skannar den med BankID, och integrationen hämtar sin egen
refresh-token. Ingen token behöver klistras in.

## Vad som krävs

- Home Assistant 2024.6 eller senare (använder `config_entry.runtime_data`).
- En webbläsare på någon enhet i hemmet, för QR-sidan.
- Inga extra Python-beroenden i HA — `manifest.json` har `requirements: []`.
  QR-koden ritas av en vendrad ren Python-kodare (se *Attribution*), allt annat
  går på `aiohttp` och standardbiblioteket.

## Installation

Kopiera katalogen:

```sh
# från repo-roten, på maskinen som kör Home Assistant
cp -r custom_components/infomentor /sökväg/till/homeassistant/config/custom_components/
```

eller klona direkt på plats:

```sh
cd /sökväg/till/homeassistant/config/custom_components
git clone <repo-url> ha-infomentor && cp -r ha-infomentor/custom_components/infomentor .
```

Starta om Home Assistant. Integrationen dyker upp under *Inställningar →
Enheter och tjänster → Lägg till integration → InfoMentor*.

## Lägg till integrationen från telefonen

Det här är hela flödet, utan dator:

1. *Inställningar → Enheter och tjänster → Lägg till integration → InfoMentor*.
2. Välj **BankID – skanna QR-kod på en annan enhet**. (Har du bara en telefon,
   välj **BankID – på den här telefonen** i stället, se nedan.)
3. Home Assistant visar en länk: *Öppna QR-koden i en webbläsare*. Öppna den på
   den enhet som ska visa QR-koden — en surfplatta, en gammal telefon, en dator.
   Sidan innehåller bara QR-koden och en statusrad, inget annat.
4. Skanna QR-koden med BankID på telefonen som har BankID. Koden roteras
   automatiskt; sidan visar *Godkänn i BankID* när du har skannat.
5. När BankID är godkänt visar sidan *Klart – gå tillbaka till Home Assistant*,
   och Home Assistant går vidare. Står dialogen fortfarande kvar på QR-steget
   trycker du **Skicka**: den väntar då klart (högst fyra minuter) och hoppar
   vidare.
6. Har kontot flera länkade barn får du välja vilka som ska få entiteter.
   Klart — integrationen sköter resten och förnyar sig själv.

Länken till QR-sidan är en engångsbijett: den slutar fungera (HTTP 404) så fort
inloggningen är klar, avbruten eller har tajmat ut, och den ger inte åtkomst
till något annat. Den bygger på Home Assistants konfigurerade externa URL om en
sådan finns, annars den interna — den måste alltså gå att nå från enheten som
ska visa QR-koden.

### Bara en telefon?

En telefon kan inte skanna sin egen skärm. Välj därför **BankID – på den här
telefonen**: då visar sidan en **Öppna BankID**-knapp i stället för en QR-kod,
och BankID-appen startas direkt på samma telefon. Resten är identiskt (öppna
länken, tryck på knappen, godkänn, gå tillbaka till Home Assistant).

## Alternativ: klistra in en refresh-token

Finns kvar som avancerat alternativ (*Klistra in refresh-token (avancerat)*) för
den som redan har en token, eller om BankID-vägen inte kan användas. En sådan
token skapas med [`infomentor-mcp`](https://github.com/wicol/infomentor-mcp):

```sh
TMP_STATE="$(mktemp -d)"
PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
XDG_STATE_HOME="$TMP_STATE" \
  uv run infomentor-mcp auth login          # BankID i webbläsaren, en gång

jq -r .refresh_token "$TMP_STATE/infomentor-mcp/mobile-credentials.json"
rm -rf "$TMP_STATE"
```

`PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` tvingar fram filen i
stället för systemets nyckelring, så att MCP:ns egen inloggning inte skrivs
över.

### Låt MCP:ns egen grant vara

**Återanvänder du MCP:ns befintliga refresh-token kan rotation göra att MCP:n
tappar sin.** InfoMentor returnerar en ny refresh-token vid förnyelse och den
gamla slutar gälla: två klienter på samma grant slår ut varandra. Använd därför
BankID-flödet här — det skapar en egen grant — och låt MCP:n behålla sin.

Samma sak i andra riktningen: om MCP:n loggar in igen och roterar bort sin grant
påverkas inte HA:s post (den har en egen). Skulle HA:s token ändå sluta
accepteras startar integrationen ett reauth-flöde i stället för att tyst sluta
uppdatera.

## Konfiguration

1. Välj inloggningssätt — BankID (QR eller telefon) är standard.
2. Integrationen växlar den hämtade koden mot ett access-token, öppnar en
   Hub-session och gör **en** läsning (barnlistan) för att verifiera. Fungerar
   det inte sparas ingenting, och felet visas i formuläret.
3. Har kontot flera länkade barn får du välja vilka som ska få entiteter.
   En config entry täcker alltså ett InfoMentor-konto (en vårdnadshavare).

### Reauth

När refresh-tokenen inte längre accepteras (HTTP 400/401 från token-endpointen)
sätter Home Assistant posten i *reauthentication required* och visar **samma
BankID-väg** — QR eller telefon, inte token-klistret. Entiteterna slutar aldrig
tyst uppdatera: `sensor.<barn>_session` står kvar och visar `reauth_required`
med orsaken i attributet `notification`.

Ett BankID som tillhör ett annat konto (annan barnuppsättning) avvisas i
reauth-flödet, så att fel inloggning inte kan ersätta en fungerande post.

### Options

*Inställningar → Enheter och tjänster → InfoMentor → Konfigurera*:

- **Barn** — vilka barn som ska ha entiteter.
- **Fritidsschemat** — avstängt som standard. Slås det på hämtas
  fritidsregistreringar för de kommande sju dagarna och blir händelser i
  barnets kalender. Det kostar ytterligare en förfrågan per barn och vecka.

## Entiteter

Ett device per barn, med barnets namn som device-namn (det är hela poängen —
det syns bara i HA:s register och i entitets-id, aldrig i loggar).

| Entitet | Typ | Innehåll |
| --- | --- | --- |
| `calendar.<barn>_skola` | kalender | Kalenderhändelser idag → +30 dagar. All-day-händelser blir datumspann. Med fritidsschemat påslaget läggs `Fritids 07:00–17:00` in som händelser. |
| `sensor.<barn>_olasta_skolhandelser` | sensor | Antal notifieringar med state `New`. Attributet `notifications` listar de senaste titlarna. |
| `sensor.<barn>_senaste_nyhet` | sensor | Senaste nyhetens titel, med `published_at` (och `news_id`) som attribut. |
| `sensor.<barn>_nasta_handelse` | sensor | Nästa kalenderhändelses titel, med `start`, `end`, `all_day` som attribut. |
| `sensor.<barn>_session` | sensor | `ok` eller `reauth_required`. Attribut: `last_update`, `last_update_success`, `include_childcare`, `notification`. |
| `sensor.infomentor_senaste_uppdatering` | sensor | Tidpunkt för senaste lyckade poll (device-class `timestamp`, ett device för själva kontot). |

## Så fungerar BankID-inloggningen

Samma väg som `infomentor-mcp` verifierat i skarpt läge, portad till `aiohttp`:

1. `GET https://sso.infomentor.se/login.ashx?idp=stockholm_par` — redirect- och
   formulärkedjan följs till Stockholmssidans BankID-sida.
2. `GET …?initialize=qr` startar en BankID-order och ger den första
   QR-payloaden. (Telefonvarianten använder `initialize=url` och får i stället
   en autostart-länk till BankID-appen.)
3. Ordern pollas (`?verifyorder=<order>`) ungefär en gång i sekunden, i högst
   fyra minuter. Varje svar kan innehålla en ny QR-payload som sidan ritar om.
   Vid `OK` följs SAML-kedjan så att en Hub-cookie-session bildas, och sessionen
   verifieras med en läsning av Hub-adressen (barnlistan).
4. `POST https://hub.infomentor.se/account/pair/GetAuthenticationData` ger
   appens `authGuid`. Svaret kontrolleras hårt: bara de verifierade
   `im.infomentor.se`/`api-im.infomentor.se`-adresserna och klienten
   `notificationapp` godtas.
5. `GET https://im.infomentor.se/Authentication/Authentication/LoginOAuth2` med
   `authGuid`, `DeviceIdentifier` (nytt UUID per inloggning),
   `DeviceFriendlyName=Home Assistant`, `DevicePlatform=Android`,
   `scope=IM2-API-NOTIFICATION`,
   `redirect_uri=InfomentorNotification://oauth2Callback`,
   `client_id=notificationapp`, `response_type=code` och `isSandBox=false`.
   Svaret ska vara en redirect till appens callback med en `code`.
6. `POST https://im.infomentor.se/Authentication/OAuth2/Token` (form-encoded,
   `grant_type=authorization_code`, `client_id=notificationapp`,
   `client_secret=NONE`, `redirect_uri=InfomentorNotification://oauth2Callback`)
   växlar koden mot access- och refresh-token. Bara refresh-tokenen sparas i
   config entryn.

**Varför BankID först?** `LoginOAuth2` kräver ett `authGuid`, och `authGuid`
delas bara ut av discovery, som i sin tur kräver en autentiserad Hub-session.
App-auktoriseringen kan alltså inte startas utan en genomförd BankID-inloggning
— därför kommer QR-steget före anropet i steg 5, inte före steg 1.

Därefter sköter den vanliga klienten all datahämtning:

1. `POST https://im.infomentor.se/Authentication/OAuth2/Token` (form-encoded)
   med `grant_type=refresh_token`, `refresh_token`, `client_id=notificationapp`,
   `client_secret=NONE`, `scope=IM2-API-NOTIFICATION`. Ger access-token (600 s)
   och en eventuellt roterad refresh-token.
2. `GET https://api-im.infomentor.se/NA1/Authentication/sso` med
   `Authorization: Bearer *** ger en inloggnings-URL.
3. Integrationen följer SAML-redirect- och formulärkedjan från den URL:en så att
   `aiohttp`-sessionens cookie-jar får en Hub-session.
4. Data läses med cookies från `https://hub.infomentor.se`:
   `/Communication/News/GetNewsList`,
   `/NotificationApp/NotificationApp/GetNotifications`,
   `/calendarv2/calendarv2/getentries`,
   `/TimeRegistration/TimeRegistration/GetTimeRegistrations/` och
   `/Account/PupilSwitcher/SwitchPupil/{id}`.

Bearer-token skickas aldrig vidare till web-inloggningen, redirects följs bara
till `*.infomentor.se` / `*.stockholm.se`, och inga tokens, QR-payloads,
`authGuid`-värden, koder eller barnnamn hamnar i loggrader.

### QR-vyn: vad den skyddar och hur den städas

- **URL:en är hemligheten.** Varje inloggningsförsök får en egen väg,
  `/api/infomentor/bankid/<32 slumpade byte>` (256 bitar från `secrets`), och
  den får nås utan inloggning just för att den *är* engångsbiljetten. Inget
  annat ligger bakom den.
- **Bara QR och status.** Sidan innehåller en `<canvas>`, en statusrad och — i
  telefonvarianten — en BankID-knapp. Inga skoluppgifter, tokens, cookies eller
  enhets-id. `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `X-Content-Type-Options: nosniff` och en strikt CSP utan externa resurser.
- **Kortlivad.** Vägen läggs i en tabell i minnet när försöket startar och tas
  bort när det tar slut: lyckat, avbrutet (`ERROR`/`CANCELLED` i BankID),
  timeout (fyra minuter) eller när dialogen stängs eller överges. Därefter
  svarar både sidan och dess `/frame` med 404.
- **Ingen loggning av hemligheter.** Varken URL:en, QR-payloaden, `authGuid`,
  koden eller tokens skrivs till loggen — felmeddelanden innehåller bara steg,
  HTTP-status och en grov kategori.
- **Egen cookie-burk.** BankID-inloggningen använder en egen, tillfällig
  `aiohttp`-session vars connector stängs när försöket är slut, så
  inloggningscookies läcker inte in i den session den körande klienten använder.

## Kända begränsningar

- **Polling var 15:e minut, ingen push.** Nya läxor och ändringar syns inom en
  kvart. InfoMentors push-notifikationer används inte.
- **Refresh-tokenens livslängd över veckor är oprövad.** Att access-tokenen
  förnyas efter faktisk utgång utan BankID är verifierat i skarpt läge av
  `infomentor-mcp`, men hur länge en refresh-token lever innan den måste förnyas
  med BankID är inte känt. Räkna med att reauth kan behövas då och då.
- **QR-koden kräver en skärm till** (eller telefonvarianten). Sidan måste nås
  från enheten som visar koden, så Home Assistant behöver en URL som den
  enheten kan använda — särskilt bakom reverse proxy.
- **Inga bilagor.** Nyhetsbilagor (PDF/DOCX) hämtas inte; bara titel, text och
  publiceringsdatum.
- **En vårdnadshavare per config entry.** Två konton = två poster, och samma
  barnuppsättning kan bara läggas upp en gång (unique id är en hash av
  barn-id:na).
- **Kalenderfönstret är idag → +30 dagar**, inte hela terminen. Historik finns
  inte, eftersom varje poll annars blir dyrare än den är värd.
- All-day-händelser blir heldagsposter i HA; InfoMentors exakta sluttid för
  heldagar är inte modellerad.
- **Inget är kört mot InfoMentors skarpa API härifrån** (inga riktiga konton
  finns i utvecklingsmiljön). Se *Utveckling* för vad som är testat och vad som
  måste provas skarpt.

## Felsökning

- `sensor.<barn>_session` = `reauth_required` → kör reauth; BankID-vägen
  används automatiskt. Attributet `notification` säger varför.
- *BankID-inloggningen avbröts eller avvisades* → ordern nekades i BankID eller
  hann gå ut (en oskannad order lever i ca 30 sekunder). Dialogen visar en ny
  QR-kod att försöka med.
- *BankID-inloggningen tog för lång tid* → ingen skanning och godkännande inom
  fyra minuter.
- QR-sidan säger *Sidan är stängd* → försöket är slut; gå tillbaka till Home
  Assistant, som redan visar nästa steg eller en ny QR.
- `cannot_connect` i config flowen → nätverksfel mot InfoMentor, inte
  inloggningsfel. Prova igen.
- Allt blir `unavailable` men sessionen säger `ok` → titta i HA-loggen efter
  `infomentor`; felmeddelandena är avsiktligt innehållslösa (inga tokens, inga
  personuppgifter), men de skiljer på nätverksfel, HTTP-status och oväntat
  svarsformat.

## Attribution

BankID- och app-auktoriseringsprotokollet är portat till `aiohttp` från
[`infomentor-mcp`](https://github.com/wicol/infomentor-mcp) (MIT):
`src/infomentor_mcp/login/web_qr.py`, `auth/stockholm_bankid.py` och
`auth/mobile_pairing.py`, med `docs/mobile-auth.md` som dokumentation. Här är
det omskrivet för Home Assistants config flow, och inget från det projektet
krävs vid körning.

QR-kodaren i `custom_components/infomentor/qrcodegen.py` är
[QR Code generator library](https://www.nayuki.io/page/qr-code-generator-library)
av Project Nayuki (MIT), vendrad oförändrad — bara en lint-rad är tillagd i
filhuvudet. Den behövs för att integrationen ska kunna rita QR-koden utan
runtime-beroenden.

## Utveckling

```sh
uv venv .venv-test
uv pip install --python .venv-test/bin/python pytest-homeassistant-custom-component aiohttp ruff
.venv-test/bin/python -m pytest -q
.venv-test/bin/ruff check custom_components tests --line-length 100
```

Testerna kör en riktig Home Assistant-core i testläge
(`pytest-homeassistant-custom-component`), laddar integrationen som custom
component och verifierar:

- att config flow, options flow och reauth-flödena skapar/uppdaterar poster —
  både BankID-vägen (QR och telefon) och den avancerade token-vägen,
- att QR-vyns livscykel håller: sidan och `/frame` svarar utan inloggning medan
  försöket pågår, innehåller bara QR/status, ger 404 för okända vägar och för
  den egna vägen efter avslutat, avbrutet, timat ut eller övergivet försök,
- att timeout, avbrott och misslyckad kodväxling ger fel i formuläret och
  **ingen** config entry,
- att hela BankID-kedjan (SSO-formulär → order → polling → SAML → discovery →
  app-auktorisering → kodväxling) går mot en lokal fejkserver med exakt form på
  anropen, och att config entryn då får den hämtade refresh-tokenen,
- att en avvisad refresh-token startar reauth och ett nätverksfel ger retry,
- att hela auth-kedjan för datahämtningen (token-endpoint → SSO-bridge →
  SAML-formulär → Hub-läsning) fungerar mot samma typ av fejkserver,
- att QR-kodaren ger en fullständig QR-symbol (modulstorlek, hittarmönster),
- att entitets-id:na blir `calendar.<barn>_skola` m.fl. och att tillstånd och
  attribut stämmer,
- att manifest och översättningar hänger ihop.

QR-kodaren är dessutom provad utanför testsviten: matrisen ritades upp som en
bitmapp och lästes tillbaka med en oberoende dekoder (OpenCV `QRCodeDetector`),
vilket gav exakt samma payload. Sidans JavaScript är syntaxkontrollerat med
`node --check`.

Det som **inte** kan verifieras utan en riktig HA-installation och ett riktigt
konto: att Stockholm/BankID accepterar en QR-order från HA:s IP, att SAML-kedjan
i praktiken landar i en Hub-cookie-session, att discovery svarar med ett
`authGuid`, att app-auktoriseringen ger en `code` som token-endpointen tar emot,
att QR-sidan går att nå från telefonen via den konfigurerade URL:en (särskilt
bakom reverse proxy), att fältnamnen i kalender/fritidssvar stämmer med dagens
API, och hur länge refresh-tokenen lever.


## Öppet: BankID-inloggningen i HA

QR-flödet fungerar fram till godkännandet: sidan visar en giltig kod, BankID
approverar ordern (pollningen når `OK`). Därefter laddas sidan om och Stockholms
SSO svarar med **BankID:s väljarsida** (`login003.stockholm.se/…/NECSadcmbid`,
28 801 tecken, inget SAML-formulär) i stället för en autentiserad session, och
Hubben svarar 302 på `timetable/timetable/appData`.

Samma utfall gäller den dedikerade samma-enhet-ordern (`initialize=url`), och
även den externa referensklienten (`infomentor-mcp auth login --method url`)
misslyckas. Det pekar på överlämningen BankID → SiteMinder, inte på HA-porten.

Att göra:
- jämför anropet som *avslutar* inloggningen med `infomentor-mcp`
  (`auth/stockholm_bankid.py:finish`) rad för rad — särskilt om SSO:n kräver
  ytterligare ett `verifyorder`-poll innan omladdning, eller en annan URL än
  den utan parametrar,
- fråga Wictor om han någonsin får väljarsidan efter ett godkänt försök,
- verifiera på en andra skärm när sådan finns; tills dess kör HA på en lånad
  refresh-token som delas med `infomentor-mcp` (de kan rotera ut varandra).
