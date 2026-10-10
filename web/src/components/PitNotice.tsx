import { api } from "../api";
import { useAsync } from "../lib";
import { Notice } from "./ui";

/** 时间点一致性提示（BR-18）：任一用于回测的数据域不一致时，在结果页面顶部说明原因与影响。 */
export default function PitNotice() {
  const { data } = useAsync(() => api.pit(), []);
  if (!data) return null;
  if (data.point_in_time) return <Notice tone="accent" title="时间点一致">回测使用的数据均按当时实际可获取的版本。</Notice>;
  return (
    <Notice tone="warn" title={`${data.label}：结果可能偏乐观`}>
      <ul className="list-disc pl-5">
        {data.reasons.map((r) => <li key={r}>{r}</li>)}
      </ul>
      <a className="text-accent underline" href="/data">在数据台查看各数据域的三个时间</a>
    </Notice>
  );
}
