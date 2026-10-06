/** Read-only probe for the Alibaba ICBU product publish page. */

function assertBrowserTab(tab) {
  if (!tab?.playwright?.evaluate) {
    throw new Error("A bound in-app browser tab is required");
  }
}

export async function probeAlibabaPublishPage(tab) {
  assertBrowserTab(tab);
  return await tab.playwright.evaluate(() => {
    const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const visible = (node) => {
      if (!(node instanceof Element)) return false;
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const fnv1a = (text) => {
      let hash = 0x811c9dc5;
      for (let i = 0; i < text.length; i += 1) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
      }
      return (hash >>> 0).toString(16).padStart(8, "0");
    };
    const bodyText = clean(document.body?.innerText || "");
    const captcha = /验证码|安全验证|人机验证|滑块|请完成验证/i.test(bodyText);
    const domainOk = location.hostname === "post.alibaba.com";

    const selectorReport = (selector) => {
      const nodes = [...document.querySelectorAll(selector)];
      return {
        selector,
        count: nodes.length,
        visible_count: nodes.filter(visible).length,
        unique_visible: nodes.filter(visible).length === 1,
      };
    };

    const criticalSelectors = [
      "input#productTitle",
      "input#minOrderQuantity",
      "input#pkgWeight",
      ".sell-catProp-item",
      ".posting-field-image-selector .image-upload-list",
    ].map(selectorReport);

    const controls = [...document.querySelectorAll("input, textarea, select, [contenteditable='true']")];
    const controlCounts = controls.reduce((acc, node) => {
      const type = node.tagName === "INPUT" ? (node.getAttribute("type") || "text").toLowerCase() : node.tagName.toLowerCase();
      acc[type] = (acc[type] || 0) + 1;
      return acc;
    }, {});

    const categoryProperties = [...document.querySelectorAll(".sell-catProp-item")].map((row, index) => {
      const input = row.querySelector("input, textarea, select");
      const lines = (row.innerText || "").split("\n").map(clean).filter(Boolean);
      return {
        index,
        label: lines[0] || null,
        input_name: input?.getAttribute("name") || null,
        input_id: input?.id || null,
        current_value: input?.value || null,
        visible: visible(row),
      };
    });

    const imageContainer = document.querySelector(".posting-field-image-selector .image-upload-list");
    const imageSlots = imageContainer ? [...imageContainer.querySelectorAll("#image-placeholder")].length : 0;
    const fileInputs = [...document.querySelectorAll("input[type='file']")].map((input) => ({
      name: input.getAttribute("name"),
      id: input.id || null,
      accept: input.getAttribute("accept"),
      visible: visible(input),
    }));
    const headings = [...document.querySelectorAll("h1, h2, h3, h4, [class*='section-title'], [class*='module-title']")]
      .filter(visible)
      .map((node) => clean(node.innerText))
      .filter(Boolean)
      .slice(0, 100);

    const fingerprintInput = JSON.stringify({
      hostname: location.hostname,
      pathname: location.pathname,
      controlCounts,
      criticalSelectors,
      imageSlots,
      categoryNames: categoryProperties.map((item) => item.input_name),
      headings,
    });

    return {
      schema_version: "1.0",
      captured_at: new Date().toISOString(),
      url: location.href,
      title: document.title,
      domain_ok: domainOk,
      captcha_detected: captcha,
      read_only: true,
      page_fingerprint: fnv1a(fingerprintInput),
      summary: {
        controls: controls.length,
        control_counts: controlCounts,
        category_property_rows: categoryProperties.length,
        main_image_slots: imageSlots,
        mounted_file_inputs: fileInputs.length,
      },
      critical_selectors: criticalSelectors,
      category_properties: categoryProperties,
      file_inputs: fileInputs,
      headings,
      safe_to_continue_read_only: domainOk && !captcha,
      safe_to_write: false,
    };
  }, undefined, { timeoutMs: 15000 });
}

