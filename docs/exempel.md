## Exempel: automationer och dashboard

Repot innehåller två färdiga exempel: en blueprint som skickar en notis när
något nytt kommer från skolan, och en Lovelace-vy som visar det senaste. Båda
bygger enbart på entiteterna ovan och kan användas som de är eller som
utgångspunkt.

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

### Om långa meddelanden

Android kapar långa texter i en notis, även när notisen är expanderad. Vill du
alltid se hela meddelandet kan du dela upp texten i två notiser och skicka dem i
omvänd ordning — del 2 först, del 1 sist — så att del 1 hamnar överst i
notislistan.

### Attributen i mallar

Attributen är tänkta att användas i automationer och mallar, till exempel:

```yaml
{{ state_attr('sensor.barn_nasta_handelse', 'start') }}
{{ state_attr('sensor.barn_olasta_skolhandelser', 'notifications')[0].subject }}
```
