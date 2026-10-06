import { createHash } from 'node:crypto';

const sha256 = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

export class SyntheticLocalizedAdapter {
  constructor(page, expectedLocales) {
    this.page = page;
    this.expected = new Map(expectedLocales.map(row => [row.locale, row]));
  }

  async enumerateLocales() {
    const rows = await this.page.locator('#language-list button[data-locale]').evaluateAll(buttons => buttons.map(button => ({
      locale:button.dataset.locale,
      label:button.textContent.trim(),
      aria:button.getAttribute('aria-label'),
      visible:!!(button.offsetWidth || button.offsetHeight || button.getClientRects().length),
    })));
    const observed = new Map();
    for (const row of rows) {
      if (!row.visible || observed.has(row.locale)) throw new Error(`ambiguous or hidden locale control: ${row.locale}`);
      observed.set(row.locale, row);
    }
    if (observed.size !== this.expected.size || [...this.expected].some(([locale, item]) => observed.get(locale)?.label !== item.label)) {
      throw new Error('visible locale set/labels differ from the reviewed 18-locale fixture');
    }
    return rows;
  }

  async fillLocale(item) {
    const language = this.page.locator(`#language-list button[data-locale="${item.locale}"]`, {hasText:item.label});
    if (await language.count() !== 1) throw new Error(`locale locator is not unique: ${item.locale}`);
    await language.click();
    const section = this.page.locator(`#editor-host section[data-locale="${item.locale}"]`);
    const beforeEpoch = Number(await section.getAttribute('data-render-epoch'));
    const input = this.page.getByRole('textbox', {name:`${item.label} product title`, exact:true});
    if (await input.count() !== 1) throw new Error(`localized textbox is not unique: ${item.locale}`);
    const staleHandle = await input.elementHandle();
    const direction = (await input.getAttribute('dir') || '').toUpperCase();
    if (direction !== item.direction) throw new Error(`direction mismatch ${item.locale}: ${direction}`);
    await input.fill(item.title);
    await this.page.waitForFunction(({locale, epoch}) => {
      const node = document.querySelector(`#editor-host section[data-locale="${locale}"]`);
      return node && Number(node.dataset.renderEpoch) > epoch;
    }, {locale:item.locale,epoch:beforeEpoch});
    const detached = await staleHandle.evaluate(node => !node.isConnected);
    await staleHandle.dispose();
    if (!detached) throw new Error(`stale handle survived full DOM replacement: ${item.locale}`);
    const current = this.page.getByRole('textbox', {name:`${item.label} product title`, exact:true});
    const value = await current.inputValue();
    if (value !== item.title) throw new Error(`readback mismatch: ${item.locale}`);
    const currentSection = this.page.locator(`#editor-host section[data-locale="${item.locale}"]`);
    const renderEpoch = Number(await currentSection.getAttribute('data-render-epoch'));
    const counter = this.page.getByRole('status', {name:`${item.label} title counter`, exact:true});
    const rawCounterText = (await counter.innerText()).trim();
    const match = /^(\d+)\s*\/\s*(\d+)$/.exec(rawCounterText);
    if (!match) throw new Error(`counter text is not parseable: ${item.locale}`);
    const observedCount = Number(match[1]); const maximum = Number(match[2]);
    if (maximum !== item.maximum || observedCount > maximum) {
      throw new Error(`counter mismatch/overflow: ${item.locale}`);
    }
    return {
      locale:item.locale, liveLanguageLabel:item.label, direction:item.direction,
      renderEpoch, controlInstanceSha256:sha256({locale:item.locale,renderEpoch,label:item.label}),
      value, valueSha256:sha256(value), rawCounterText, observedCount, maximum,
      unit:'PLATFORM_COUNTER', postWriteCounterReadback:true,
    };
  }

  async verifyLocaleReadback(item, {select = true} = {}) {
    const language = this.page.locator(`#language-list button[data-locale="${item.locale}"]`, {hasText:item.label});
    if (await language.count() !== 1) throw new Error(`reopen locale locator is not unique: ${item.locale}`);
    if (select) await language.click();
    const input = this.page.getByRole('textbox', {name:`${item.label} product title`, exact:true});
    const value = await input.inputValue();
    const direction = (await input.getAttribute('dir') || '').toUpperCase();
    const counter = this.page.getByRole('status', {name:`${item.label} title counter`, exact:true});
    const rawCounterText = (await counter.innerText()).trim();
    const match = /^(\d+)\s*\/\s*(\d+)$/.exec(rawCounterText);
    if (value !== item.title || direction !== item.direction || !match) throw new Error(`reopen readback mismatch: ${item.locale}`);
    if (Number(match[2]) !== item.maximum || Number(match[1]) > Number(match[2])) throw new Error(`reopen counter mismatch: ${item.locale}`);
    return {locale:item.locale,valueSha256:sha256(value),direction,rawCounterText,observedCount:Number(match[1]),maximum:Number(match[2])};
  }

  async readAll(expected) {
    const result = [];
    for (const item of expected) result.push(await this.fillLocale(item));
    return result;
  }
}
