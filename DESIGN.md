# 供应商与合同管理系统 — 设计文档

> 单用户、轻量级、Python 全栈。本文档为实施前的设计基线，所有人审阅确认后再进入编码阶段。

---

## 1. 项目结构（monorepo）

```
supplier/
├── backend/                       # FastAPI 后端
│   ├── app/
│   │   ├── main.py                # 应用入口、CORS、路由挂载
│   │   ├── config.py              # 环境变量加载（pydantic-settings）
│   │   ├── database.py            # SQLAlchemy engine + session
│   │   ├── deps.py                # FastAPI 依赖（auth、db）
│   │   ├── models/                # SQLAlchemy ORM 模型
│   │   │   ├── supplier.py
│   │   │   ├── quote.py           # Quote / QuoteLine / PriceTier
│   │   │   ├── contract.py
│   │   │   └── lookup.py          # Country / ProductCategory
│   │   ├── schemas/               # Pydantic 请求/响应模型
│   │   ├── api/                   # 路由
│   │   │   ├── auth.py
│   │   │   ├── suppliers.py
│   │   │   ├── quotes.py
│   │   │   ├── contracts.py
│   │   │   ├── search.py
│   │   │   ├── files.py
│   │   │   └── lookups.py
│   │   ├── services/              # 业务逻辑
│   │   │   ├── auth.py            # JWT 签发/校验
│   │   │   ├── parser.py          # PDF / Excel → 文本
│   │   │   ├── extraction.py      # Claude 抽取
│   │   │   └── storage.py         # 对象存储封装（S3/R2 兼容）
│   │   └── utils/
│   ├── alembic/                   # 数据库迁移
│   ├── tests/
│   ├── pyproject.toml
│   └── .env.example
├── frontend/                      # Next.js 14 (App Router)
│   ├── src/
│   │   ├── app/
│   │   │   ├── login/page.tsx
│   │   │   ├── (app)/             # 受保护路由组
│   │   │   │   ├── layout.tsx     # 侧边栏 + 鉴权 guard
│   │   │   │   ├── page.tsx       # Dashboard（合同到期提醒）
│   │   │   │   ├── suppliers/
│   │   │   │   ├── quotes/
│   │   │   │   ├── contracts/
│   │   │   │   └── compare/       # 比价检索
│   │   │   └── api/auth/[...]     # 仅做 cookie 中转（可选）
│   │   ├── components/ui/         # shadcn 组件
│   │   ├── components/
│   │   ├── lib/api.ts             # fetch 封装 + JWT 注入
│   │   └── types/
│   ├── package.json
│   └── .env.local.example
├── docker-compose.yml             # 本地 postgres
├── .gitignore
└── README.md
```

---

## 2. 数据库 schema

### 设计要点
- **不建 users 表**：单用户，凭据走环境变量
- **价格三层**：Quote（一次报价）→ QuoteLine（国家×产品）→ PriceTier（阶梯）
- **append-only 价格历史**：新建 Quote 时，自动把同一 (supplier, country, category, route_name) 的旧 active Quote 的 `effective_to` 设为新 quote 的 `effective_from`
- **国家**用 ISO 3166-1 alpha-2 码（NG / TZ / PK …），便于多语言展示
- **产品类别**用枚举 + 自定义扩展（首次部署预置 sms / voice / did）

### SQL 建表

```sql
-- 国家字典
CREATE TABLE countries (
  code        CHAR(2) PRIMARY KEY,
  name_zh     VARCHAR(64) NOT NULL,
  name_en     VARCHAR(64) NOT NULL
);

-- 产品类别字典（可扩展）
CREATE TABLE product_categories (
  id          SERIAL PRIMARY KEY,
  code        VARCHAR(32) UNIQUE NOT NULL,    -- sms, voice, did
  name        VARCHAR(64) NOT NULL,
  default_unit VARCHAR(32)                    -- "per SMS", "per minute"
);

-- 供应商
CREATE TABLE suppliers (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          VARCHAR(255) NOT NULL,
  contact_name  VARCHAR(255),
  contact_email VARCHAR(255),
  contact_phone VARCHAR(64),
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 报价单（一次提交）
CREATE TABLE quotes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  supplier_id     UUID NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
  source_type     VARCHAR(16) NOT NULL,           -- text | pdf | excel
  source_file_url TEXT,
  source_file_name VARCHAR(255),
  raw_text        TEXT,
  effective_from  DATE NOT NULL,
  effective_to    DATE,                            -- NULL = 当前生效
  status          VARCHAR(16) NOT NULL DEFAULT 'draft', -- draft | active | archived
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_quotes_supplier ON quotes(supplier_id);
CREATE INDEX idx_quotes_active   ON quotes(status, effective_to);

-- 报价行（国家 × 产品类别 × 路由）
CREATE TABLE quote_lines (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quote_id      UUID NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
  country_code  CHAR(2) NOT NULL REFERENCES countries(code),
  category_id   INT NOT NULL REFERENCES product_categories(id),
  route_name    VARCHAR(128),                      -- 可选：MTN / Airtel / Premium
  currency      CHAR(3) NOT NULL,                  -- USD / CNY / EUR
  unit          VARCHAR(32) NOT NULL,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_quote_lines_quote  ON quote_lines(quote_id);
CREATE INDEX idx_quote_lines_search ON quote_lines(country_code, category_id);

-- 阶梯价
CREATE TABLE price_tiers (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quote_line_id UUID NOT NULL REFERENCES quote_lines(id) ON DELETE CASCADE,
  min_volume    INTEGER NOT NULL DEFAULT 0,
  max_volume    INTEGER,                           -- NULL = 无上限
  price         NUMERIC(18, 6) NOT NULL,
  CHECK (max_volume IS NULL OR max_volume >= min_volume)
);
CREATE INDEX idx_price_tiers_line ON price_tiers(quote_line_id);

-- 合同
CREATE TABLE contracts (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contract_type   VARCHAR(16) NOT NULL,            -- purchase | sales
  contract_no     VARCHAR(64),
  party_name      VARCHAR(255) NOT NULL,           -- 对手方
  supplier_id     UUID REFERENCES suppliers(id),   -- 采购合同时关联供应商
  start_date      DATE NOT NULL,
  end_date        DATE NOT NULL,
  amount          NUMERIC(18, 2),
  currency        CHAR(3),
  product_summary TEXT,                             -- 自由文本：合同涉及的产品
  file_url        TEXT,
  file_name       VARCHAR(255),
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_contracts_type ON contracts(contract_type);
CREATE INDEX idx_contracts_end  ON contracts(end_date);
```

### 价格更新流程示意
1. 用户为 Supplier A 上传新报价 → 创建 status=draft 的 Quote
2. 抽取 + 人工确认后 → 调用 `POST /api/quotes/{id}/activate`
3. 服务端事务内：
   - 找出 Supplier A 中所有 (country, category, route) 与新 Quote 的 lines 重叠的、当前 active 的旧 Quote
   - 把它们的 `effective_to` 设为新 quote 的 `effective_from - 1 day`、`status = archived`
   - 新 quote 设为 `status = active`
4. 查询当前价：`WHERE status = 'active' AND (effective_to IS NULL OR effective_to >= today)`

---

## 3. API 设计

### 鉴权
```
POST /api/auth/login        body: { username, password }
                            res:  { token, expires_at }
GET  /api/auth/me           header: Authorization: Bearer <token>
                            res:  { username }
```
- JWT，HS256，24h 过期
- 密码用 `secrets.compare_digest` 做常时间比较
- 前端把 token 存 `httpOnly` cookie（通过 Next.js Route Handler 中转）或 localStorage（更简单，但安全性弱）

### 供应商
```
GET    /api/suppliers?q=&country=&category=&page=1&page_size=20
POST   /api/suppliers       body: { name, contact_name?, contact_email?, contact_phone?, notes? }
GET    /api/suppliers/{id}  含派生字段：covered_countries[], covered_categories[], active_quotes_count
PATCH  /api/suppliers/{id}
DELETE /api/suppliers/{id}  级联删 quotes
```

### 报价
```
GET    /api/quotes?supplier_id=&status=&country=&category=
POST   /api/quotes          手动创建 draft（不上传文件）
GET    /api/quotes/{id}     含 lines + tiers
PATCH  /api/quotes/{id}     仅 draft 可编辑
DELETE /api/quotes/{id}
POST   /api/quotes/{id}/activate    把 draft → active，归档同位旧 quote
POST   /api/quotes/{id}/archive     手动归档
```

### 报价上传 + 抽取（核心三步）
```
1) POST /api/quotes/upload         multipart: file, supplier_id
   → { upload_token, source_type, raw_text, source_file_url }
   （文件存对象存储，文本即时抽取返回）

2) POST /api/quotes/extract        body: { upload_token? | raw_text, supplier_id }
   → { suggested_lines: [
        { country_code, country_raw, category_hint, route_name,
          currency, unit, tiers: [{min_volume, max_volume, price}], notes }
      ] }
   （调 Claude，仅返回建议，不入库）

3) POST /api/quotes/confirm        body: {
        supplier_id, source_type, source_file_url?, source_file_name?,
        raw_text?, effective_from, notes?,
        lines: [{ country_code, category_id, route_name?, currency, unit, notes?,
                  tiers: [{min_volume, max_volume, price}] }, ...]
      }
   → { quote_id, status: 'draft' }
   （创建 draft；用户在前端点"激活"再触发 activate）
```

### 比价检索（核心价值）
```
GET /api/search/prices?country=NG&category=sms&volume=10000&currency=USD
→ [
    {
      supplier_id, supplier_name,
      quote_id, quote_line_id,
      country_code, category_code, route_name,
      currency, unit,
      matched_tier: { min_volume, max_volume, price },
      effective_from, effective_to
    },
    ...
  ]
  按 matched_tier.price 升序；只查 status=active 且当前生效的 quote
  volume 用于挑选阶梯（落入 [min, max] 的那一档）
  currency 可选；不传则返回原币种，前端展示时附标签
```

### 合同
```
GET    /api/contracts?type=&q=&expiring_within_days=30
POST   /api/contracts
GET    /api/contracts/{id}
PATCH  /api/contracts/{id}
DELETE /api/contracts/{id}
```

### 文件上传
```
POST /api/files/upload         multipart: file
  → { url, name, size, mime }
  （后端转存对象存储；返回可访问 URL）
```

### 字典
```
GET /api/countries
GET /api/categories
POST /api/categories           body: { code, name, default_unit? }
```

---

## 4. Claude 抽取设计

### 模型选择
`claude-haiku-4-5-20251001` — 抽取任务结构化、上下文不长，Haiku 性价比最优。如遇复杂多页 PDF 抽取质量不够，可临时切到 `claude-sonnet-4-6`。

### 调用方式
- 用 Anthropic SDK 的 **tool use** 强制结构化输出（比让模型直接返回 JSON 更稳）
- 启用 **prompt caching**：system prompt + few-shot examples 长期缓存

### System Prompt（草稿）
```
You are extracting supplier price quote data from raw text. The text comes from a
PDF, Excel, or plain-text quotation. Extract every (country, product, price) entry
into structured JSON via the `submit_quote_lines` tool.

Rules:
- Country: prefer ISO 3166-1 alpha-2 codes (NG, TZ, PK, ...). If you cannot map
  reliably, leave country_code null and put the original string in country_raw.
- Currency: ISO 4217 code (USD, CNY, EUR). If only a symbol like "$" appears and
  the document context does not disambiguate, leave null.
- Tiered pricing example: "1-1000: $0.05 / 1000+: $0.04" → two tiers.
- Single price → one tier with min_volume=0, max_volume=null.
- Route/operator hints: include strings like "MTN", "Airtel", "Premium" in route_name.
- Category hint: classify as one of: sms | voice | did | other.
- If a field is missing or unclear, return null. Do not invent values.
- If the document is not a price quote at all, return an empty `lines` array.
```

### Tool schema（传给 Claude 的 input_schema）
```json
{
  "name": "submit_quote_lines",
  "description": "Submit the structured price-quote lines extracted from the document.",
  "input_schema": {
    "type": "object",
    "properties": {
      "lines": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "country_code":  {"type": ["string", "null"], "description": "ISO 3166-1 alpha-2"},
            "country_raw":   {"type": ["string", "null"]},
            "category_hint": {"type": ["string", "null"], "enum": ["sms", "voice", "did", "other", null]},
            "route_name":    {"type": ["string", "null"]},
            "currency":      {"type": ["string", "null"], "description": "ISO 4217"},
            "unit":          {"type": ["string", "null"]},
            "tiers": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "min_volume": {"type": "integer", "minimum": 0},
                  "max_volume": {"type": ["integer", "null"]},
                  "price":      {"type": "number"}
                },
                "required": ["min_volume", "price"]
              }
            },
            "notes": {"type": ["string", "null"]}
          },
          "required": ["tiers"]
        }
      }
    },
    "required": ["lines"]
  }
}
```

### 抽取后处理
- `country_code` 为空但 `country_raw` 存在 → 在前端"待确认"页让用户手动选
- `category_hint` 映射到 `product_categories.code`，匹配不到则提示新增类别
- 所有 `tiers` 按 `min_volume` 升序检查重叠，重叠则在前端高亮警告

---

## 5. 环境变量

### `backend/.env.example`
```bash
# Database
DATABASE_URL=postgresql+psycopg://supplier:supplier@localhost:5432/supplier

# Auth (单用户固定凭据)
AUTH_USERNAME=admin
AUTH_PASSWORD=please-change-me
JWT_SECRET=generate-with-openssl-rand-hex-32
JWT_EXPIRES_HOURS=24

# Claude API
ANTHROPIC_API_KEY=sk-ant-xxx
CLAUDE_MODEL=claude-haiku-4-5-20251001

# 对象存储（S3 兼容；推荐 Cloudflare R2 或阿里云 OSS）
STORAGE_PROVIDER=r2                          # r2 | s3 | oss
STORAGE_BUCKET=supplier-files
STORAGE_REGION=auto
STORAGE_ENDPOINT=https://<account>.r2.cloudflarestorage.com
STORAGE_ACCESS_KEY=xxx
STORAGE_SECRET_KEY=xxx
STORAGE_PUBLIC_BASE_URL=https://files.example.com   # 用户访问文件的公开 URL 前缀

# CORS
FRONTEND_ORIGIN=http://localhost:3000
```

### `frontend/.env.local.example`
```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

---

## 6. .gitignore 关键条目
```
# python
__pycache__/
*.pyc
.venv/
.env

# node
node_modules/
.next/
.env.local

# 上传/临时
uploads/
*.log
.DS_Store
```

---

## 7. 待你确认的设计决策

1. **国家字典**：预置三国（NG / TZ / PK）足够起步，还是希望初始化时灌入全量 250 个国家？
2. **产品类别**：预置 `sms / voice / did` 三类够不够？还有其他常用类别？
3. **币种处理**：比价时要不要做实时汇率换算（需引入汇率源）？还是只按原币种展示，用户自己心算？
4. **文件保留**：原始上传文件是否永久保留？还是 N 个月后自动归档？
5. **合同提醒**：仅 dashboard 高亮，还是要邮件提醒？（邮件需 SMTP 配置）
6. **字符集**：后端日志和注释用中文还是英文？（个人偏好；不影响功能）

---

回复确认或针对以上 6 点给出选择，我就进入 Step 2 —— 搭骨架并按 Week 1/Week 2 节奏实现。
