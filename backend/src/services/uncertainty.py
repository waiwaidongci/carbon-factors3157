import math
import numpy as np

M64=np.uint64(0xFFFFFFFFFFFFFFFF)
SPLIT_A=np.uint64(0x9E3779B97F4A7C15)
SPLIT_B=np.uint64(0xBF58476D1CE4E5B9)
SPLIT_C=np.uint64(0x94D049BB133111EB)
TWO27=np.float64(134217728.0)   # 2**27
TWO53=np.float64(9007199254740992.0)  # 2**53
UCLAMP=np.float64(1.0-1e-12)
BISECTION_STEPS=55
QUEUE_KEY='sampling-jobs'
PROBS=(0.025,0.5,0.975)


def _u64(x):
 return np.uint64(x)&M64


def splitmix64(z):
 '''向量化 SplitMix64：uint64 计数器 → uint64 伪随机字。'''
 z=_u64(z+SPLIT_A)
 z=_u64((z^_u64(z>>np.uint64(30)))*SPLIT_B)
 z=_u64((z^_u64(z>>np.uint64(27)))*SPLIT_C)
 return _u64(z^_u64(z>>np.uint64(31)))


def uniform53(seed,word_base,n):
 '''生成 n 个 53 位 [0,1) 均匀数，消耗连续 2n 个 SplitMix64 字（第 i 个均匀数取字 word_base+2i、+2i+1）。'''
 offsets=(np.arange(n,dtype=np.uint64)*np.uint64(2))
 idx=np.add(offsets,np.uint64(int(word_base)),dtype=np.uint64)
 hi27=splitmix64(np.add(idx,np.uint64(seed),dtype=np.uint64))
 lo26=splitmix64(np.add(idx,np.uint64(int(seed)+1),dtype=np.uint64))
 u=(hi27>>np.uint64(37)).astype(np.float64)/TWO27+(lo26>>np.uint64(38)).astype(np.float64)/TWO53
 return np.minimum(u,UCLAMP)


def gaussian_column(seed,col_word_base,total_n,start,size):
 '''Box–Muller 单列标准正态。列内字空间分段：u1 流占 [col_base, col_base+2n)，
 u2 流占 [col_base+2n, col_base+4n)，两流各自连续。
 块 [start,start+size) 的基址 = col_base+2·start 与 col_base+2n+2·start，
 只随 start 平移、与 size/分块方式无关，故任意切分都逐样本复现同一序列。'''
 u1_base=np.uint64(int(col_word_base)+2*start)
 u2_base=np.uint64(int(col_word_base)+2*total_n+2*start)
 u1=np.maximum(uniform53(seed,u1_base,size),np.float64(1e-12))
 u2=uniform53(seed,u2_base,size)
 return np.sqrt(np.float64(-2.0)*np.log(u1))*np.cos(np.float64(2.0*math.pi)*u2)


def validate_correlation(keys,corr):
 '''相关矩阵：方阵、对角1、对称、取值[-1,1]、正定（Cholesky 成功）。非法即抛 ValueError。'''
 if not isinstance(corr,list) or not corr or any(not isinstance(r,list) for r in corr):
  raise ValueError('对数相关矩阵必须为二维数组')
 k=len(keys)
 if len(corr)!=k or any(len(r)!=k for r in corr):raise ValueError('对数相关矩阵阶数必须与因子数一致')
 a=np.array([[float(x) for x in r] for r in corr],dtype=np.float64)
 if not np.all(np.isfinite(a)):raise ValueError('相关矩阵含非数值')
 if np.any(np.abs(a)>1.0+1e-9):raise ValueError('相关系数必须位于[-1,1]')
 if max(abs(a[i,i]-1.0) for i in range(k))>1e-9:raise ValueError('相关矩阵对角线必须为1')
 if np.max(np.abs(a-a.T))>1e-9:raise ValueError('相关矩阵必须对称')
 try:np.linalg.cholesky(a)
 except np.linalg.LinAlgError:raise ValueError('相关矩阵非正定，拒绝排队（请检查因子相关性）')
 return a


def build_basis(sources,policy,means,stds,corr,seed):
 '''把冻结底稿/口径编译成与抽样次数无关的静态依据，并校验矩阵与对数正态参数。'''
 from src.services.calculation import CONVERSIONS
 factors=policy['factors']
 activities=sorted({s['activity'] for s in sources})
 if set(activities)!=set(means):raise ValueError('因子均值必须且仅覆盖底稿出现的全部活动：'+'、'.join(activities))
 if set(stds)!=set(means):raise ValueError('因子标准差键必须与均值一致')
 order=sorted(means)
 for a in order:
  if not isinstance(means[a],(int,float)) or not math.isfinite(means[a]) or means[a]<=0:raise ValueError('因子均值必须为正数：'+a)
  if not isinstance(stds[a],(int,float)) or not math.isfinite(stds[a]) or stds[a]<0:raise ValueError('因子标准差不能为负：'+a)
  if a not in factors:raise ValueError('口径缺少活动因子：'+a)
 cmat=validate_correlation(order,corr)
 depts=sorted({s['department'] for s in sources})
 dept_index={d:i for i,d in enumerate(depts)}
 qty=[];ai=[];di=[]
 for s in sources:
  f=factors[s['activity']];key=(s['unit'],f['unit'])
  if key not in CONVERSIONS:raise ValueError('单位无法转换：'+s['unit']+' → '+f['unit'])
  qty.append(float(s['quantity'])*CONVERSIONS[key]);ai.append(order.index(s['activity']));di.append(dept_index[s['department']])
 params=[]
 for a in order:
  m=float(means[a]);sd=float(stds[a])
  if sd==0:mu,sigma=math.log(m),0.0
  else:
   var=math.log1p((sd/m)**2);sigma=math.sqrt(var);mu=math.log(m)-var/2
  params.append((mu,sigma))
 return {'activities':order,'departments':depts,'qty':np.array(qty,dtype=np.float64),
  'activity_idx':np.array(ai,dtype=np.int64),'department_idx':np.array(di,dtype=np.int64),
  'params':params,'chol':np.linalg.cholesky(cmat),'seed':int(seed),'k':len(order)}


def draw_factors(basis,start,size):
 '''size 个联合对数正态因子样本：对数空间 Cholesky 相关，同次抽样各部门共用因子。
 全局字计数器按列分段（每列 4·总样本数字），样本只取决于 seed 与全局样本序号，与分块无关。'''
 k=basis['k'];n=basis.get('total_n')
 z=np.empty((size,k),dtype=np.float64)
 for j in range(k):z[:,j]=gaussian_column(basis['seed'],4*n*j,n,start,size)
 corr=z@basis['chol'].T
 out=np.empty((size,k),dtype=np.float64)
 for j,(mu,sigma) in enumerate(basis['params']):
  out[:,j]=np.exp(mu+sigma*corr[:,j]) if sigma>0 else np.exp(mu)
 return out


def chunk_emissions(basis,start,size,total_n):
 '''因子样本 × 冻结活动量：先逐记录合并，再按全体/部门聚合。'''
 basis={**basis,'total_n':total_n}
 factors=draw_factors(basis,start,size)
 line=basis['qty'][None,:]*factors[:,basis['activity_idx']]  # size × 记录数
 total=line.sum(axis=1)
 nd=len(basis['departments']);dept=np.zeros((size,nd),dtype=np.float64)
 for j in range(nd):
  mask=(basis['department_idx']==j)
  if mask.any():dept[:,j]=line[:,mask].sum(axis=1)
 return factors,total,dept


def point_estimate(basis):
 '''均值因子（确定性）下的点估计，与随机区间并列展示。'''
 means=np.array([math.exp(mu+sigma**2/2) for mu,sigma in basis['params']])
 line=basis['qty']*means[basis['activity_idx']]
 dept=np.array([line[basis['department_idx']==j].sum() if (basis['department_idx']==j).any() else 0.0 for j in range(len(basis['departments']))])
 return float(line.sum()),[float(x) for x in dept]


def streaming_quantiles(basis,n,chunk):
 '''精确最近秩分位数。阈值二分定位秩所在区间，再用一次常量内存扫描取出真实第 k 小样本值
 （不小于收敛上界 hi 的最小观测值），故结果是精确次序统计量而非近似阈值；
 内存 O(块大小×因子数)，与 n 无关；样本流按全局序号重放，与分块大小、完成顺序无关。'''
 nd=len(basis['departments']);groups=1+nd;targets=groups*3
 ranks=np.array([max(1,math.ceil(p*n)) for _ in range(groups) for p in PROBS],dtype=np.int64)
 lo=np.full(targets,np.inf);hi=np.full(targets,-np.inf)
 start=0
 while start<n:
  size=min(chunk,n-start);_,total,dept=chunk_emissions(basis,start,size,n)
  vals=np.column_stack((total,dept)).repeat(3,axis=1)
  lo=np.minimum(lo,vals.min(axis=0));hi=np.maximum(hi,vals.max(axis=0))
  start+=size
 for _ in range(BISECTION_STEPS):
  mid=(lo+hi)/2;counts=np.zeros(targets,dtype=np.int64);start=0
  while start<n:
   size=min(chunk,n-start);_,total,dept=chunk_emissions(basis,start,size,n)
   vals=np.column_stack((total,dept)).repeat(3,axis=1)
   counts+=(vals<=mid).sum(axis=0)
   start+=size
  lo=np.where(counts<ranks,mid,lo);hi=np.where(counts>=ranks,mid,hi)
 # 精确提取：二分保持不变量 #(<=hi)>=k，第 k 小即不小于 hi 的最小观测值；同遍计数核验
 exact=np.full(targets,np.inf);counts_le_hi=np.zeros(targets,dtype=np.int64);start=0
 while start<n:
  size=min(chunk,n-start);_,total,dept=chunk_emissions(basis,start,size,n)
  vals=np.column_stack((total,dept)).repeat(3,axis=1)
  counts_le_hi+=(vals<=hi).sum(axis=0)
  cand=np.where(vals>=hi,vals,np.inf).min(axis=0)
  exact=np.minimum(exact,cand)
  start+=size
 if np.any(counts_le_hi<ranks):raise RuntimeError('分位数秩不变量被破坏，放弃最终化')
 q=np.where(np.isinf(exact),hi,exact)  # 全部样本相等时，第 k 小即该常量
 return q.reshape(groups,3)


def assemble_result(basis,job):
 n=job.sample_count;chunk=job.chunk_size
 nd=len(basis['departments'])
 sum_total=0.0;comp_total=0.0;sum_dept=np.zeros(nd);comp_dept=np.zeros(nd);done=0
 start=0
 while start<n:  # 均值用 Kahan 补偿求和，按全局序号流式累计，同样与分块无关
  size=min(chunk,n-start);_,total,dept=chunk_emissions(basis,start,size,n)
  y=float(total.sum())-comp_total;t=sum_total+y;comp_total=(t-sum_total)-y;sum_total=t
  for j in range(nd):
   y=float(dept[:,j].sum())-comp_dept[j];t=sum_dept[j]+y;comp_dept[j]=(t-sum_dept[j])-y;sum_dept[j]=t
  done+=size;start+=size
 q=streaming_quantiles(basis,n,chunk)
 point_total,point_dept=point_estimate(basis)
 intervals=[]
 for gi,name in enumerate(['全体']+basis['departments']):
  intervals.append({'口径对象':name,'点估计kgCO2e':point_total if gi==0 else point_dept[gi-1],
   '样本均值kgCO2e':(sum_total/n) if gi==0 else (sum_dept[gi-1]/n),
   'P2.5下限kgCO2e':float(q[gi,0]),'P50中位数kgCO2e':float(q[gi,1]),'P97.5上限kgCO2e':float(q[gi,2])})
 factors_meta=[]
 for j,a in enumerate(basis['activities']):
  mu,sigma=basis['params'][j]
  mean=math.exp(mu+sigma**2/2);sd=0.0 if sigma==0 else mean*math.sqrt(math.exp(sigma**2)-1)
  factors_meta.append({'活动':a,'对数均值mu':mu,'对数标准差sigma':sigma,'还原均值kg每单位':mean,'还原标准差kg每单位':sd})
 return {'summary':{'抽样次数':n,'已完成样本':done,'分块大小':chunk,'随机种子':basis['seed'],
   '全体点估计kgCO2e':point_total,'全体样本均值kgCO2e':sum_total/n,
   '全体P2.5kgCO2e':float(q[0,0]),'全体P97.5kgCO2e':float(q[0,2])},
  'intervals':intervals,'factors':factors_meta,
  'method':'按因子均值/标准差与对数相关矩阵构造联合对数正态（对数空间Cholesky相关），同次抽样各部门共用因子；先合并全部排放，再对全体与部门分别取精确最近秩95%区间（秩=ceil(p·n)的2.5%/97.5%次序统计量）。分块流式，计数器RNG同种子结果与分块大小、完成顺序无关。',
  'warnings':['Monte Carlo经验区间，非解析置信区间；样本数越大越稳定','相关矩阵经正定校验通过后才允许排队']}
