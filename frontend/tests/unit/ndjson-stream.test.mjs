import test from "node:test";
import assert from "node:assert/strict";

import { readNdjsonStream } from "../../app/ndjson-stream.ts";

/** 把若干段**字节**拼成一条可读流。分段刻意由调用方决定：这个读取循环的全部难点
 *  就在「行边界与分片边界不重合」时还得对。 */
function byteStream(...chunks) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(...chunks) {
  const lines = [];
  await readNdjsonStream(byteStream(...chunks), (line) => { lines.push(line); });
  return lines;
}

test("保活空行不交给调用方", async () => {
  assert.deepEqual(
    await collect('{"event":"started"}\n', "\n", "\n", '{"event":"gone"}\n'),
    ['{"event":"started"}', '{"event":"gone"}'],
  );
});

test("被分片切开的一个 JSON 对象仍按整行交付", async () => {
  assert.deepEqual(
    await collect('{"event":"prog', 'ress","trace_offset":0}', "\n"),
    ['{"event":"progress","trace_offset":0}'],
  );
});

test("末尾不带换行的那一行不会丢", async () => {
  // 终态帧正是最后一行：少了流结束后的残段补发，调用方只会看到「提前结束」。
  assert.deepEqual(
    await collect('{"event":"progress"}\n{"event":"final"}'),
    ['{"event":"progress"}', '{"event":"final"}'],
  );
});

test("一个 UTF-8 字符被拆在两段字节之间也不会解码成替换字符", async () => {
  const encoded = new TextEncoder().encode('{"summary":"检索"}\n');
  const split = 12;
  const lines = [];
  await readNdjsonStream(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoded.slice(0, split));
        controller.enqueue(encoded.slice(split));
        controller.close();
      },
    }),
    (line) => { lines.push(line); },
  );
  assert.deepEqual(lines, ['{"summary":"检索"}']);
});

test("同一分片里的多行按序、逐行 await 交付", async () => {
  const order = [];
  await readNdjsonStream(
    byteStream('{"n":1}\n{"n":2}\n{"n":3}\n'),
    async (line) => {
      order.push(`start:${line}`);
      await new Promise((resolve) => setTimeout(resolve, 0));
      order.push(`end:${line}`);
    },
  );
  assert.deepEqual(order, [
    'start:{"n":1}', 'end:{"n":1}',
    'start:{"n":2}', 'end:{"n":2}',
    'start:{"n":3}', 'end:{"n":3}',
  ]);
});

test("consume 抛出时整条读取 reject", async () => {
  await assert.rejects(
    readNdjsonStream(byteStream('{"event":"error"}\n'), () => {
      throw new Error("feed error");
    }),
    { message: "feed error" },
  );
});
