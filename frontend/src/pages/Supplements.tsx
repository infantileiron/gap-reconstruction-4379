import {useState, useEffect} from 'react';
import * as api from '../api/supplements';
import * as datasetsApi from '../api/datasets';
import {useResource} from '../hooks/useResource';
import {refreshResources} from '../stores/resources';
import PageTitle from '../components/common/PageTitle';
import Notice from '../components/common/Notice';
import {supplementStatusLabels, supplementActionLabels} from '../constants/enums';
import type {User, SupplementBatch} from '../types/entities';

const fmt = (v: number | null) => (v === null || v === undefined ? '缺失' : Number(v).toFixed(4));

export default function Supplements({user}: { user: User }) {
  const {data: batches, error} = useResource(api.list, []);
  const {data: dsList} = useResource(datasetsApi.list, []);
  const [datasetId, setDatasetId] = useState('ds-stations');
  const [revision, setRevision] = useState(1);
  const [name, setName] = useState('测站迟到读数/更正值补传');
  const [csv, setCsv] = useState('station,time_hour,value,withdrawn\n');
  const [message, setMessage] = useState('');
  const [openId, setOpenId] = useState('');
  const canEdit = user.role !== 'viewer';

  useEffect(() => {
    if (dsList.length && !dsList.some(d => d.id === datasetId)) setDatasetId(dsList[0].id);
  }, [dsList]);
  const selectedDs = dsList.find(d => d.id === datasetId);
  const visibleBatches = batches.filter(b => b.dataset_id === datasetId);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMessage('');
    try {
      const batch = await api.create({
        dataset_id: datasetId, source_revision: revision, name,
        csv_text: csv,
      });
      setMessage(batch.replayed
        ? `同修订号同内容重复补传，已幂等返回原批次 ${batch.id}，未产生新版本。`
        : batch.status === 'noop'
          ? '补传已接受但没有实际变化（空值不覆盖/同值重复），数据版本不变。'
          : `补传已合并：数据版本 v${batch.data_version_before} → v${batch.data_version_after}。`);
      setOpenId(batch.id);
      refreshResources();
    } catch (e: any) {
      setMessage(e.message);
    }
  }

  return <>
    <PageTitle chapter="07" title="按测站与时刻合并补传，不重导整份档案"
      description="以来源修订号裁决新旧：更高修订才出版本；同版异值整批拒绝；空值不覆盖实测，显式撤回才变回缺失。重复补传幂等，并发补传行锁保护，运行只认发起时冻结的版本。"/>
    <Notice error={error || message}/>

    {canEdit && <form className="paper form-grid" onSubmit={submit}>
      <label>目标观测数据
        <select aria-label="目标观测数据" value={datasetId} onChange={e => setDatasetId(e.target.value)}>
          {dsList.map(d => <option key={d.id} value={d.id}>{d.name}（当前 v{d.current_version} / 修订 {d.current_revision}）</option>)}
        </select>
      </label>
      <label>来源修订号（正整数，须高于已落地最新修订 {selectedDs?.current_revision ?? 0}）
        <input type="number" min={1} step={1} aria-label="来源修订号" value={revision}
               onChange={e => setRevision(+e.target.value)}/>
      </label>
      <label className="wide">批次名称
        <input aria-label="批次名称" maxLength={100} value={name} onChange={e => setName(e.target.value)}/>
      </label>
      <label className="wide">补传内容（CSV：station,time_hour,value；可加 withdrawn 列，1/true 表示显式撤回；value 留空=空值不覆盖）
        <textarea style={{height: 150}} aria-label="补传CSV" value={csv} onChange={e => setCsv(e.target.value)}
          placeholder={'station,time_hour,value,withdrawn\nLP-01,60,11.16,0\nLP-02,200,,1'}/>
      </label>
      <button className="primary">按修订号合并补传</button>
    </form>}

    <section className="paper">
      <h2>补传批次与合并明细 · {selectedDs?.name ?? datasetId}（当前 v{selectedDs?.current_version ?? 0}）</h2>
      {visibleBatches.length === 0 && <p>该数据集暂无补传批次。分析员可通过上方入口提交迟到读数或更正值。</p>}
      {[...visibleBatches].reverse().map((b: SupplementBatch) => <article className="record record-column" key={b.id}>
        <div className="record-head" onClick={() => setOpenId(openId === b.id ? '' : b.id)} role="button" tabIndex={0}>
          <div>
            <b>{b.name}</b>
            <small>{b.dataset_id} · 来源修订 {b.source_revision} · {b.item_count} 条 · 改变 {b.change_count} 条 · {b.actor} · {b.created_at.slice(0, 19).replace('T', ' ')}</small>
          </div>
          <div className="actions">
            <span className={'badge ' + b.status}>{supplementStatusLabels[b.status] || b.status}{b.replayed ? '（幂等重播）' : ''}</span>
            <button type="button">{openId === b.id ? '收起明细 ▲' : '合并明细 ▼'}</button>
          </div>
        </div>
        {b.status === 'rejected' && <div className="notice" style={{marginTop: 12}}>
          整批拒绝原因：{b.reject_reason}
          {b.reject_detail.length > 0 && <ul>
            {b.reject_detail.map((c, i) => <li key={i}>{c.station} @ {c.time_hour}h：已落地 {fmt(c.held_value)}，本次 {fmt(c.incoming_value)}</li>)}
          </ul>}
        </div>}
        {openId === b.id && <div className="table-scroll" style={{marginTop: 14}}>
          <table>
            <thead><tr><th>测站</th><th>时刻(h)</th><th>合并前</th><th>本次来值</th><th>合并后</th><th>动作</th><th>修订</th></tr></thead>
            <tbody>
              {b.details.map((d, i) => <tr key={i}>
                <td>{d.station}</td>
                <td>{d.time_hour}</td>
                <td>{fmt(d.old_value)}</td>
                <td>{d.withdrawn ? '撤回' : fmt(d.incoming_value)}</td>
                <td><b>{fmt(d.new_value)}</b></td>
                <td>{supplementActionLabels[d.action] || d.action}</td>
                <td>{d.source_revision}</td>
              </tr>)}
            </tbody>
          </table>
          <small>数据版本：v{b.data_version_before} → v{b.data_version_after}（无变化时版本不动）。原始导入观测 rows 永不改写，当前生效值由合并结果 current_rows 承载。</small>
        </div>}
      </article>)}
    </section>
  </>;
}
