# InfoMentor för Home Assistant

En liten, läsbar custom integration som visar barnets InfoMentor-data i Home
Assistant: skolnyheter, notifieringar, kalender och (valfritt) fritidsschema.

Integrationen är **inte** gjord för HACS. Den installeras genom att kopiera
katalogen — eller genom att klona repot — till `config/custom_components/` och
starta om Home Assistant.

Den är **read-only**: den läser skolnyheter, notifieringar, kalendern och
(eventuellt) fritidsregistreringar. Ingenting skrivs tillbaka till InfoMentor,
och ingen BankID-inloggning sker i Home Assistant.

## Vad som krävs

- Home Assistant 2024.6 eller senare (använder `config_entry.runtime_data`).
- Ett eget **refresh-token** från InfoMentors mobilgrant (se nedan).
- Inga extra Python-beroenden i HA — `manifest.json` har `requirements: []`,
  allt går på `aiohttp` och standardbiblioteket.

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

## Skapa en egen refresh-token

InfoMentor använder BankID, som inte kan köras obevakat inne i Home Assistant.
Det som fungerar är mobil-appens OAuth2-grant: en refresh-token som förnyar
sessionen utan BankID. Den skapas med hjälp av
[`infomentor-mcp`](https://github.com/wicol/infomentor-mcp) (Wictors MCP) —
men **skapa en egen grant**, återanvänd inte MCP:ns.

Kör inloggningen med en tillfällig `XDG_STATE_HOME`. Varje inloggning genererar
nämligen ett eget UUID och en egen grant, och med en tillfällig state-katalog
hamnar `mobile-credentials.json` där i stället för i MCP:ns vanliga katalog:

```sh
TMP_STATE="$(mktemp -d)"
PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
XDG_STATE_HOME="$TMP_STATE" \
  uv run infomentor-mcp auth login          # BankID i webbläsaren, en gång

# plocka ut refresh-token (access-token behövs inte)
jq -r .refresh_token "$TMP_STATE/infomentor-mcp/mobile-credentials.json"

rm -rf "$TMP_STATE"
```

`PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` tvingar fram filen i
stället för systemets nyckelring. Utan den skriver `infomentor-mcp` till
nyckelringen, där MCP:ns egen post redan ligger — och då kan MCP:n tappa sin
inloggning.

### Viktigt: låt MCP:ns token vara

**Återanvänder du MCP:ns befintliga refresh-token kan rotation göra att MCP:n
tappar sin.** InfoMentor returnerar en ny refresh-token vid förnyelse, och den
gamla slutar gälla. Den här integrationen sparar den roterade tokenen i sin
config entry varje gång den förnyar — men MCP:n har ingen aning om det, och
nästa gång *den* förnyar tar den över på samma sätt. Två klienter på samma
grant slår alltså ut varandra. Skapa därför en grant för Home Assistant och låt
MCP:n behålla sin.

Samma sak gäller i andra riktningen: om du senare kör `auth login` igen i MCP:n
och den kastar/roterar grants, kan HA-posten behöva ett nytt token. Då startar
integrationen ett reauth-flöde (se nedan) i stället för att tyst sluta
uppdatera.

## Konfiguration

1. Klistra in refresh-token i config flowen.
2. Integrationen växlar tokenen mot ett access-token, öppnar en Hub-session och
   gör en läsning för att verifiera. Fungerar det inte får du felmeddelandet
   *InfoMentor avvisade refresh-token* och ingenting sparas.
3. Har kontot flera länkade barn får du välja vilka som ska få entiteter.
   En config entry täcker alltså ett InfoMentor-konto (en vårdnadshavare).

### Reauth

När refresh-tokenen inte längre accepteras (HTTP 400/401 från token-endpointen)
sätter Home Assistant posten i *reauthentication required* och visar ett
formulär där du klistrar in ett nytt token. Entiteterna slutar aldrig tyst
uppdatera: `sensor.<barn>_session` står kvar och visar `reauth_required` med
orsaken i attributet `notification`.

Ett token för ett annat konto (annan barnuppsättning) avvisas i reauth-flödet,
så att fel inloggning inte kan ersätta en fungerande post.

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

## Så fungerar autentiseringen

Samma väg som `infomentor-mcp` verifierat i skarpt läge:

1. `POST https://im.infomentor.se/Authentication/OAuth2/Token` (form-encoded)
   med `grant_type=refresh_token`, `refresh_token`, `client_id=notificationapp`,
   `client_secret=NONE`, `scope=IM2-API-NOTIFICATION`. Ger access-token (600 s)
   och en eventuellt roterad refresh-token.
2. `GET https://api-im.infomentor.se/NA1/Authentication/sso` med
   `Authorization: Bearer <access>` ger en inloggnings-URL.
3. Integrationen följer SAML-redirect- och formulärkedjan från den URL:en så att
   `aiohttp`-sessionens cookie-jar får en Hub-session.
4. Data läses med cookies från `https://hub.infomentor.se`:
   `/Communication/News/GetNewsList`,
   `/NotificationApp/NotificationApp/GetNotifications`,
   `/calendarv2/calendarv2/getentries`,
   `/TimeRegistration/TimeRegistration/GetTimeRegistrations/` och
   `/Account/PupilSwitcher/SwitchPupil/{id}`.

Bearer-token skickas aldrig vidare till web-inloggningen, redirects följs bara
till `*.infomentor.se` / `*.stockholm.se`, och inga tokens eller barnnamn
hamnar i loggrader.

## Kända begränsningar

- **Polling var 15:e minut, ingen push.** Nya läxor och ändringar syns inom en
  kvart. InfoMentors push-notifikationer används inte.
- **Refresh-tokenens livslängd över veckor är oprövad.** Att access-tokenen
  förnyas efter faktisk utgång utan BankID är verifierat i skarpt läge av
  `infomentor-mcp`, men hur länge en refresh-token lever innan den måste
  förnyas med BankID är inte känt. Räkna med att reauth kan behövas då och då.
- **Inga bilagor.** Nyhetsbilagor (PDF/DOCX) hämtas inte; bara titel, text och
  publiceringsdatum.
- **En vårdnadshavare per config entry.** Två konton = två poster, och samma
  barnuppsättning kan bara läggas upp en gång (unique id är en hash av
  barn-id:na).
- **Kalenderfönstret är idag → +30 dagar**, inte hela terminen. Historik finns
  inte, eftersom varje poll annars blir dyrare än den är värd.
- All-day-händelser blir heldagsposter i HA; InfoMentors exakta sluttid för
  heldagar är inte modellerad.
- Inga tester har körts mot InfoMentors skarpa API i den här sessionen (inga
  riktiga tokens finns här). Se "Utveckling" för vad som är testat.

## Felsökning

- `sensor.<barn>_session` = `reauth_required` → kör reauth och klistra in ett
  nytt refresh-token. Attributet `notification` säger varför.
- `cannot_connect` i config flowen → nätverksfel mot InfoMentor, inte
  tokenfel. Prova igen.
- Allt blir `unavailable` men sessionen säger `ok` → titta i HA-loggen efter
  `infomentor`; felmeddelandena är avsiktligt innehållslösa (inga tokens, inga
  personuppgifter), men de skiljer på nätverksfel, HTTP-status och oväntat
  svarsformat.

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

- att config flow, options flow och reauth-flödena skapar/uppdaterar poster,
- att entitets-id:na blir `calendar.<barn>_skola` m.fl. och att tillstånd och
  attribut stämmer,
- att ett avvisat refresh-token startar reauth och ett nätverksfel ger retry,
- att hela auth-kedjan (token-endpoint → SSO-bridge → SAML-formulär → Hub-läsning)
  fungerar mot en lokal fejkserver, inklusive exakt form på token-anropet och
  att roterade tokens rapporteras vidare,
- att manifest och översättningar hänger ihop.

Det som **inte** kan verifieras utan en riktig HA-installation och ett riktigt
konto: att InfoMentor accepterar ett riktigt refresh-token från HA, att
SAML-kedjan i praktiken landar i en Hub-cookie-session från en annan IP, att
fältnamnen i kalender/fritidssvar stämmer med dagens API, och hur länge
refresh-tokenen lever.
