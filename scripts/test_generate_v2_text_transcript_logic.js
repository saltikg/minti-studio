#!/usr/bin/env node

const assert = require("assert");

const groupParagraphs = (words, pauseSeconds = 1.2, targetWords = 60) => {
  const groups = [];
  let current = [];
  const push = () => {
    if (current.length) {
      groups.push(current);
      current = [];
    }
  };
  words.forEach((word, index) => {
    current.push(word);
    const next = words[index + 1];
    const pause = next ? (Number(next.start || 0) - Number(word.end || word.start || 0)) : 0;
    const sentenceEnded = /[.!?\u2026]$/.test(String(word.text || "").trim());
    if (pause >= pauseSeconds || (current.length >= targetWords && sentenceEnded)) {
      push();
    }
  });
  push();
  return groups;
};

const selectionToRange = (words, indexes) => {
  const selected = [...indexes].sort((a, b) => a - b);
  const first = selected[0];
  const last = selected[selected.length - 1];
  return {
    start: Math.max(0, Number(words[first].start || 0) - 0.04),
    end: Number(words[last].end || words[last].start || 0) + 0.08,
  };
};

(() => {
  const words = [
    { text: "One", start: 0, end: 0.3 },
    { text: "sentence.", start: 0.35, end: 0.8 },
    { text: "Next", start: 2.1, end: 2.4 },
    { text: "one", start: 2.45, end: 2.7 },
  ];
  const grouped = groupParagraphs(words);
  assert.strictEqual(grouped.length, 2, "pause >= 1.2s starts a new paragraph");
  assert.deepStrictEqual(grouped.map((group) => group.map((word) => word.text)), [["One", "sentence."], ["Next", "one"]]);

  const longWords = Array.from({ length: 62 }, (_, index) => ({
    text: index === 60 ? "done." : `w${index}`,
    start: index,
    end: index + 0.2,
  }));
  assert.strictEqual(groupParagraphs(longWords)[0].length, 61, "long paragraph breaks at sentence end after target size");

  assert.deepStrictEqual(selectionToRange(words, [1]), { start: 0.31, end: 0.88 }, "single word selection maps with padding");
  assert.deepStrictEqual(selectionToRange(words, [1, 2]), { start: 0.31, end: 2.48 }, "adjacent selected indexes map to one contiguous range");

  const duration = selectionToRange(words, [0, 1]).end - selectionToRange(words, [0, 1]).start;
  assert(duration > 0.8 && duration < 1.0, "selection duration is computed from padded range");
})();

console.log("GENERATE_V2_TEXT_TRANSCRIPT_LOGIC_OK");
