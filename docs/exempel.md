## Exempel: automationer och dashboard

Repot innehåller färdiga exempel: en blueprint som skickar en notis när något
nytt kommer från skolan, en Lovelace-vy som visar det senaste, en automation som
delar långa meddelanden och ett kort som bläddrar i historiken. Alla bygger
enbart på entiteterna ovan och kan användas som de är eller som utgångspunkt.

### Notis när olästa händelser ökar (blueprint)

Blueprinten
[`blueprints/automation/infomentor/skolnotis.yaml`](../blueprints/automation/infomentor/skolnotis.yaml)
skickar en notis när antalet olästa händelser går upp. Meddelandets ämne blir
notisens rubrik och meddelandets text blir brödtexten. Saknas text — till
exempel vid en ren kalenderhändelse — visas antalet nya händelser i stället. Ett
tryck på notisen öppnar den sida du anger.

Importera den i Home Assistant:

1. *Inställningar → Automationer och scener → Blueprints*.
2. **Importera blueprint** och klistra in URL:en:

   ```
   https://github.com/vemsom/ha-infomentor/blob/master/blueprints/automation/infomentor/skolnotis.yaml
   ```

3. Skapa en automation från blueprinten och välj:
   - **Sensor för olästa händelser** — barnets
     `sensor.<barn>_olasta_skolhandelser`.
   - **Notifieringstjänst** — tjänsten som ska skicka notisen, till exempel
     `notify.mobile_app_min_telefon`. Namnet hittas under *Utvecklarverktyg →
     Åtgärder*.
   - **Sida som öppnas vid tryck** — sökvägen till vyn, till exempel
     `/lovelace/skola` om du använder vyn nedan.
4. Spara.

Automationen reagerar på att sensorn ändrar värde och skickar bara när siffran
går upp, inte när den går ner.

### Samma automation som ren YAML

Den som hellre skriver automationen själv, utan blueprint, kan lägga in den här
direkt i `automations.yaml` eller via *Redigera som YAML*:

```yaml
alias: Skolnotis
description: Notis när antalet olästa skolhändelser ökar
trigger:
  - trigger: state
    entity_id: sensor.DITT_BARN_OLASTA_SKOLHANDELSER
condition:
  - condition: template
    value_template: >-
      {{ trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}
variables:
  items: >-
    {{ state_attr('sensor.DITT_BARN_OLASTA_SKOLHANDELSER', 'notifications') or [] }}
  messages: >-
    {{ items | selectattr('type', 'eq', 'MessageCreated')
             | selectattr('body') | list }}
action:
  - action: notify.mobile_app_min_telefon
    data:
      title: >-
        {{ messages[0].subject if messages and messages[0].subject else 'Skolan' }}
      message: >-
        {{ messages[0].body if messages else (trigger.to_state.state ~ ' nya händelser från skolan') }}
      data:
        url: /lovelace/skola
        clickAction: /lovelace/skola
mode: single
```

Byt ut `sensor.DITT_BARN_OLASTA_SKOLHANDELSER` mot barnets riktiga
entitets-id — `sensor.<barn>_olasta_skolhandelser` — och
`notify.mobile_app_min_telefon` mot din egen notifieringstjänst. Ligger vyn på
en annan sökväg ändrar du `url` och `clickAction` i `action`.

### Dashboard: skola-vy

[`examples/lovelace/skola-vy.yaml`](../examples/lovelace/skola-vy.yaml) är en
färdig vy med markdown-kort för senaste meddelande, nästa händelse, senaste
nyhet och status, plus ett kort med de underliggande entiteterna. All text
hämtas ur sensorattributen, så vyn kräver inga egna kort eller extra
integrationer.

1. Öppna instrumentpanelen och välj *Redigera instrumentpanel* → menyn uppe till
   höger → **Raw configuration editor**.
2. Klistra in filens innehåll som en vy, eller ersätt innehållet i en tom vy.
3. Byt ut `DITT_BARN` mot barnets entitets-id. Id:t syns under enheten i
   *Inställningar → Enheter och tjänster → InfoMentor*. Entitets-id skrivs med
   gemener, så `sensor.DITT_BARN_OLASTA_SKOLHANDELSER` blir till exempel
   `sensor.barn_olasta_skolhandelser`.
4. Använder du en annan sökväg än `skola` ändrar du `url` och `clickAction` i
   automationen så att notisen öppnar rätt sida.

### Hela meddelandet i delar

Android kapar en notis vid ungefär **285 tecken**, även när notisen är
expanderad — resten försvinner. Mätt på Android 15 med Home Assistants
companion-app: en testnotis med 200 numrerade fält slutade på fält 57.

[`examples/automation/skolnotis-delad.yaml`](../examples/automation/skolnotis-delad.yaml)
skickar därför hela skolmeddelandet i delar om högst 250 tecken, delade vid
ordgräns, med `(1/3)`, `(2/3)`, `(3/3)` i rubriken. Delarna skickas i **omvänd
ordning** — sista först — eftersom Android lägger den senast skickade notisen
överst. Då hamnar del 1 överst och läses först. (På iOS är det tvärtom: där
staplas den nyaste nederst, så skicka i vanlig ordning.)

Två saker till som automationen gör, och som är värda att känna till:

- Den tar det **nyaste** meddelandet, sorterat på `sent_at` — inte den första
  posten i listan. Annars skickas ett gammalt meddelande så fort antalet olästa
  ökar av en annan anledning, till exempel en nyhet.
- Den skickar bara om meddelandet är **högst tre timmar gammalt**. Blir en
  gammal post oläst igen (skolan ändrar i den, eller inloggningen markeras om)
  går ingen notis ut.

Allt räknas ut en gång i variabeln `packet`, som JSON, så att repeat-loopen kan
plocka ut en del i taget:

```yaml
packet: >-
  {%- set body = ... -%}
  {%- set ns = namespace(delar=[], rad='') -%}
  {%- for ord in body.split(' ') -%}
  {%- if ns.rad and ((ns.rad ~ ' ' ~ ord) | length) > 250 -%}
  {%- set ns.delar = ns.delar + [ns.rad] -%}{%- set ns.rad = ord -%}
  {%- else -%}{%- set ns.rad = ((ns.rad ~ ' ' ~ ord) | trim) -%}
  {%- endif -%}
  {%- endfor -%}
  {{ {'amne': ..., 'antal': delar | length, 'delar': delar} | tojson }}
```

Byt ut tre saker innan du sparar:

- `sensor.DITT_BARN_OLASTA_SKOLHANDELSER` — barnets sensor för olästa
  händelser, `sensor.<barn>_olasta_skolhandelser`. Den finns i `trigger`,
  `condition` och `variables`.
- `notify.mobile_app_DIN_TELEFON` — din notifieringstjänst, i `action`-steget.
- `/lovelace/skola` — sidan som öppnas vid tryck, i `url` och `clickAction`.

### Historik i dashboarden

[`examples/lovelace/infomentor-historik.js`](../examples/lovelace/infomentor-historik.js)
är ett eget Lovelace-kort — en JavaScript-modul utan beroenden — som visar
skolans meddelanden och nyheter med flikar och sidbläddring. Kortet läser Home
Assistants egen historik via `history/history_during_period`, alltså det
recorder redan har sparat: ingen extra integration, ingen molntjänst och inga
API-nycklar.

Flikarna visar antal poster var. Sidorna bläddras med *‹ Nyare* och *Äldre ›*,
och långa texter kortas till 260 tecken med en *Visa mer*-knapp. HTML i
nyhetstexten rensas bort. Historiken hämtas när kortet laddas och sedan var
femte minut.

Installation, tre steg:

1. Kopiera `infomentor-historik.js` till `/config/www/` på din
   Home Assistant-instans — samma mapp som andra lokala resurser.
2. *Inställningar → Instrumentpaneler → menyn uppe till höger → Resurser →
   Lägg till resurs*: `/local/infomentor-historik.js`, typ
   **JavaScript-modul**.
3. Klistra in kortet från
   [`examples/lovelace/skola-historik.yaml`](../examples/lovelace/skola-historik.yaml)
   i en vy via *Raw configuration editor*. Filen har installationsstegen i
   kommentarerna också.

Kortets nycklar:

- `message_sensor` — barnets sensor för olästa händelser,
  `sensor.<barn>_olasta_skolhandelser`. Meddelandena ligger i attributet
  `notifications`, där varje post har `subject`, `body` och `sent_at`. Kortet
  deduplicerar på tid och ämne, eftersom samma lista följer med varje
  tillståndsändring. Exempelfilen använder platshållaren
  `sensor.DITT_BARN_OLASTA_SKOLHANDELSER`.
- `news_sensor` — `sensor.<barn>_senaste_nyhet`.
- `days` — hur långt bakåt kortet letar. Standard 10.
- `page_size` — poster per sida. Standard 1, det vill säga en post i taget;
  höj värdet för färre sidor att bläddra mellan.

Minst en av `message_sensor` och `news_sensor` måste anges, annars vägrar
kortet att laddas.

Kortet kan bara visa det recordern sparar. Standard är 10 dagar; vill du
bläddra längre bakåt måste `purge_keep_days` höjas under `recorder:` i
`configuration.yaml`, vilket kostar databasutrymme. Varje post läggs till när
sensorn byter värde — en nyhet som ligger kvar som "senaste nyhet" i en vecka
blir alltså en post, inte en per poll.

### Attributen i mallar

Attributen är tänkta att användas i automationer och mallar, till exempel:

```yaml
{{ state_attr('sensor.barn_nasta_handelse', 'start') }}
{{ state_attr('sensor.barn_olasta_skolhandelser', 'notifications')[0].subject }}
```
