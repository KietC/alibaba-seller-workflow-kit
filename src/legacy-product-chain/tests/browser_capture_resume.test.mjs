import assert from "node:assert/strict";
import test from "node:test";

import {
  isChallengeState,
  safePageUrl,
} from "../scripts/browser_capture_resume.mjs";

test("safePageUrl removes query and fragment", () => {
  assert.equal(
    safePageUrl("https://www.alibaba.com/product-detail/123.html?token=x#y"),
    "https://www.alibaba.com/product-detail/123.html",
  );
  assert.throws(() => safePageUrl("https://example.com/product/123"));
});

test("challenge detection uses title, marker, and visible text", () => {
  assert.equal(isChallengeState({ title: "验证码拦截" }), true);
  assert.equal(isChallengeState({ body_text: "亲，请拖动下方滑块完成验证" }), true);
  assert.equal(isChallengeState({ challenge_markers: 1 }), true);
  assert.equal(isChallengeState({ title: "Product 123", body_text: "Specifications" }), false);
});
