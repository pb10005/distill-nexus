// @covers AC-010, AC-011
// git diff は常にフォワードスラッシュ区切りでパスを出力するが、node:pathのrelative()は
// Windows上ではバックスラッシュ区切りを返す。両者を突き合わせる前にこれで正規化する。
export function toPosixPath(p: string): string {
  return p.split("\\").join("/");
}
