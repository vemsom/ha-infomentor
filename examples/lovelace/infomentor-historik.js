/**
 * infomentor-historik — bläddra i InfoMentors meddelande- och nyhetshistorik.
 *
 * Läser Home Assistants recorder via history/history_during_period och visar
 * de senaste posterna med sidbläddring. Inga beroenden.
 *
 * Kortkonfiguration:
 *   type: custom:infomentor-historik
 *   message_sensor: sensor.DITT_BARN_OLASTA_SKOLHANDELSER  (valfri)
 *   news_sensor: sensor.DITT_BARN_SENASTE_NYHET            (valfri)
 *   days: 90
 *   page_size: 8
 */

const DEFAULTS = { days: 10, page_size: 1 };

const stripHtml = (value) => {
  if (!value) return "";
  const tmp = document.createElement("div");
  tmp.innerHTML = String(value);
  return (tmp.textContent || tmp.innerText || "").replace(/\s+\n/g, "\n").trim();
};

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("sv-SE", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
};

class InfomentorHistorik extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = { messages: [], news: [] };
    this._tab = "messages";
    this._page = 0;
    this._loaded = false;
    this._loading = false;
    this._error = null;
    this._lastFetch = 0;
    this._expanded = new Set();
  }

  setConfig(config) {
    if (!config.message_sensor && !config.news_sensor) {
      throw new Error("Ange message_sensor och/eller news_sensor");
    }
    this._config = { ...DEFAULTS, ...config };
    this._loaded = false;
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || Date.now() - this._lastFetch > 300000) {
      this._refresh();
    } else if (!this._loading) {
      this._render();
    }
  }

  async _refresh() {
    if (this._loading || !this._hass) return;
    this._loading = true;
    this._lastFetch = Date.now();
    this._error = null;
    this._render();

    const end = new Date();
    const start = new Date(end.getTime() - this._config.days * 86400000);
    const entities = [this._config.message_sensor, this._config.news_sensor].filter(Boolean);

    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: entities,
        minimal_response: false,
        no_attributes: false,
        significant_changes_only: false,
      });

      this._entries.messages = this._collectNotifications(
        result?.[this._config.message_sensor]
      );
      this._entries.news = this._collect(
        result?.[this._config.news_sensor],
        (a) => ({
          id: a.news_id || a.published_at || a.title,
          title: a.title || "Nyhet",
          body: stripHtml(a.content || ""),
          when: a.published_at || null,
        })
      );
      this._loaded = true;
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  /**
   * Meddelandena ligger som en lista i attributet `notifications` på
   * olästa-skolnändelser-sensorn. Varje historikrad bär hela listan, så
   * samma meddelande dyker upp många gånger — deduplicera på tid + ämne.
   */
  _collectNotifications(rows) {
    if (!Array.isArray(rows)) return [];
    const seen = new Map();
    for (const row of rows) {
      const list = (row.a || row.attributes || {}).notifications || [];
      for (const note of list) {
        if (!note || !note.body) continue;
        const key = `${note.sent_at || ""}|${note.subject || ""}`;
        if (seen.has(key)) continue;
        seen.set(key, {
          id: key,
          title: note.subject || note.title || "Meddelande",
          body: note.body,
          when: note.sent_at || null,
        });
      }
    }
    return [...seen.values()].sort((x, y) => {
      const tx = x.when ? Date.parse(x.when) : 0;
      const ty = y.when ? Date.parse(y.when) : 0;
      return ty - tx;
    });
  }

  _collect(rows, mapper) {
    if (!Array.isArray(rows)) return [];
    const seen = new Map();
    for (const row of rows) {
      const attrs = row.a || row.attributes || {};
      const item = mapper(attrs);
      if (!item.id || !item.body) continue;
      const key = String(item.id);
      if (!seen.has(key)) seen.set(key, item);
    }
    return [...seen.values()].sort((x, y) => {
      const tx = x.when ? Date.parse(x.when) : 0;
      const ty = y.when ? Date.parse(y.when) : 0;
      return ty - tx;
    });
  }

  _render() {
    const t = {
      messages: "Meddelanden",
      news: "Nyheter",
    };
    const counts = {
      messages: this._entries.messages.length,
      news: this._entries.news.length,
    };
    const list = this._entries[this._tab];
    const size = this._config.page_size;
    const pages = Math.max(1, Math.ceil(list.length / size));
    if (this._page >= pages) this._page = pages - 1;
    const slice = list.slice(this._page * size, this._page * size + size);

    const tabs = Object.entries(t)
      .filter(() => true)
      .map(([key, label]) => `
        <button class="tab ${key === this._tab ? "active" : ""}" data-tab="${key}">
          ${label}<span class="count">${counts[key]}</span>
        </button>`)
      .join("");

    const items = slice.map((item, index) => {
      const key = `${this._tab}:${this._page}:${index}`;
      const expanded = this._expanded.has(key);
      const long = item.body.length > 260;
      const body = expanded || !long ? item.body : `${item.body.slice(0, 260).trimEnd()}…`;
      return `
        <article class="item">
          <header>
            <h3>${this._escape(item.title)}</h3>
            <time>${this._escape(fmtDate(item.when))}</time>
          </header>
          <p>${this._escape(body).replace(/\n/g, "<br>")}</p>
          ${long ? `<button class="more" data-key="${key}">${expanded ? "Visa mindre" : "Visa mer"}</button>` : ""}
        </article>`;
    }).join("");

    const empty = this._loaded && !list.length
      ? `<p class="empty">Inget i historiken de senaste ${this._config.days} dagarna.</p>`
      : "";

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
        .tab {
          background: none; border: 1px solid var(--divider-color, #e0e0e0);
          color: var(--secondary-text-color, #727272); border-radius: 16px;
          padding: 4px 12px; font: inherit; font-size: 13px; cursor: pointer;
        }
        .tab.active {
          border-color: var(--primary-color, #03a9f4);
          color: var(--primary-color, #03a9f4);
          background: color-mix(in srgb, var(--primary-color, #03a9f4) 10%, transparent);
        }
        .count { margin-left: 6px; opacity: .7; font-size: 11px; }
        .item {
          border-top: 1px solid var(--divider-color, #e0e0e0);
          padding: 10px 0;
        }
        .item:first-of-type { border-top: none; }
        header { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
        h3 {
          margin: 0; font-size: 14px; font-weight: 600;
          color: var(--primary-text-color, #212121); line-height: 1.3;
        }
        time { font-size: 11px; color: var(--secondary-text-color, #727272); white-space: nowrap; }
        p {
          margin: 4px 0 0; font-size: 13px; line-height: 1.45;
          color: var(--primary-text-color, #212121); opacity: .88; white-space: pre-wrap;
        }
        .more, .nav button {
          background: none; border: none; padding: 2px 0; margin-top: 4px;
          color: var(--primary-color, #03a9f4); font: inherit; font-size: 12px; cursor: pointer;
        }
        .nav { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; gap: 8px; }
        .nav span { font-size: 12px; color: var(--secondary-text-color, #727272); }
        .empty, .status { font-size: 13px; color: var(--secondary-text-color, #727272); }
        .error { color: var(--error-color, #db4437); font-size: 13px; }
      </style>
      <div class="tabs">${tabs}</div>
      ${this._loading && !this._loaded ? `<p class="status">Hämtar historik…</p>` : ""}
      ${this._error ? `<p class="error">Kunde inte läsa historiken: ${this._escape(this._error)}</p>` : ""}
      ${empty}
      ${items}
      ${list.length ? `
        <div class="nav">
          <button data-page="-1" ${this._page === 0 ? "disabled" : ""}>‹ Nyare</button>
          <span>Sida ${this._page + 1} av ${pages} · ${list.length} ${this._tab === "messages" ? "meddelanden" : "nyheter"}</span>
          <button data-page="1" ${this._page >= pages - 1 ? "disabled" : ""}>Äldre ›</button>
        </div>` : ""}
    `;

    this.shadowRoot.querySelectorAll(".tab").forEach((el) =>
      el.addEventListener("click", () => {
        this._tab = el.dataset.tab;
        this._page = 0;
        this._render();
      })
    );
    this.shadowRoot.querySelectorAll(".nav button").forEach((el) =>
      el.addEventListener("click", () => {
        this._page = Math.max(0, Math.min(this._page + Number(el.dataset.page), pages - 1));
        this._render();
      })
    );
    this.shadowRoot.querySelectorAll(".more").forEach((el) =>
      el.addEventListener("click", () => {
        const key = el.dataset.key;
        if (this._expanded.has(key)) this._expanded.delete(key);
        else this._expanded.add(key);
        this._render();
      })
    );
  }

  _escape(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

if (!customElements.get("infomentor-historik")) {
  customElements.define("infomentor-historik", InfomentorHistorik);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "infomentor-historik",
  name: "InfoMentor – historik",
  description: "Bläddra i skolans meddelanden och nyheter.",
  preview: false,
});
