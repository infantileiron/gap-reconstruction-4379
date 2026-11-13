export const roleLabels:Record<string,string>={admin:'缺测复核工程师',analyst:'测站数据分析员',viewer:'环境观察员'};
export const runLabels:Record<string,string>={queued:'排队中',running:'重建中',completed:'已完成',failed:'失败',pending:'待复核',approved:'已通过',rejected:'已退回'};
export const methodLabels={rts:'RTS离线重建',pchip:'PCHIP内点插值'};
export const supplementStatusLabels:Record<string,string>={applied:'已接受并出版本',noop:'已接受·无变化',rejected:'整批拒绝'};
export const supplementActionLabels:Record<string,string>={late_fill:'迟到读数填缺口',corrected:'更正值覆盖实测',empty_ignored:'空值不覆盖',withdrawn:'显式撤回变缺失',redundant:'同值重复无变化',rejected_conflict:'同版异值冲突',rejected_with_batch:'随整批拒绝'};
