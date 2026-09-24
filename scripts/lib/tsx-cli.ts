// @covers AC-012, AC-014
// Windows上ではnpx/npm/tsx（.cmdシム）をshellオプション無しでchild_processから
// 直接spawnできない。tsxパッケージが公開しているCLIエントリを直接resolveし、
// process.execPathで呼び出すことで、どのOSでも同じ経路(node <cli.mjs> ...)にする。
import { fileURLToPath } from "node:url";

export function resolveTsxCli(): string {
  return fileURLToPath(import.meta.resolve("tsx/cli"));
}
