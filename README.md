# OpenAPI 契约兼容性审计 API

用 Python + FastAPI 实现的 OpenAPI 3.x 契约服务，提供七个接口：

- **`POST /audit`**：提交**基线版**与**候选版**两份 OpenAPI 3.x JSON 文档，服务会解析本地 `$ref`（循环安全），并按以下策略判定候选版是否向后兼容：

  - **请求侧约束不得收紧**：删除操作、新增必填请求参数 / 请求体 / 必填字段、类型变更、枚举收窄等均为破坏性变更。
  - **响应侧约束不得放宽**：删除响应状态码 / 媒体类型 / 字段、字段不再必填、类型变更、枚举放宽等均为破坏性变更。

  报告按 **JSON Pointer 稳定排序**，每条结果包含 `pointer`、`category`、`severity`、`reason`；
  无法解析的引用会以 HTTP 422 返回明确错误。

- **`POST /slice`**：提交**一份** OpenAPI 3.x 文档与一组不重复的 `METHOD /path` 选择器，
  生成只包含所选操作的**最小子契约**，并返回键序稳定的子契约及其 **SHA-256** 摘要。

- **`POST /compose`**：提交 **2～20 份**各自带 `service_id` 与 `path_prefix` 的 OpenAPI 3.x 文档
  以及合成后的 `info`，生成一份**网关契约**及**来源映射**（provenance）：路径按规范化前缀挂载合并，
  组件名 / security 键 / `operationId` / tag 均以 `service_id` 为前缀命名空间化，
  指向 `components` 的本地 `$ref` 同步改写，根级 `security` 物化到未声明该字段的操作上。

- **`POST /examples/validate`**：提交**一份** OpenAPI 3.x 文档，按各媒体类型的 Schema 校验
  所有 `requestBody` 与响应 **JSON 媒体类型**（`application/json` 及 `*+json`）上的
  `example` 与 `examples.<name>.value`（含经本地 `$ref` 引用的 Example Object）。
  请求示例出现 `readOnly` 字段、响应示例出现 `writeOnly` 字段即报错，`required` 按方向生效
  （跨 `allOf` 分支、经本地 `$ref` 的 `readOnly` / `writeOnly` 标记同样豁免对应方向的必填检查），
  区分 3.0 `nullable` 与 3.1 类型联合；纯 Schema 违规返回 `200` + `valid=false`，
  `externalValue` / 外部或悬空引用返回 `422`。

- **`POST /overlay/apply`**：提交**一份** OpenAPI 3.x 文档与 **1～50 个有序动作**，
  每个动作用**受限 JSONPath**（`$`、点成员、方括号引号成员、数组下标、对象/数组通配符，
  不支持过滤器与递归下降）定位节点，然后 `update`（对象递归合并、其余整体替换）或
  `remove`（禁止删除根节点）。同一动作先冻结全部命中再应用，数组删除按索引倒序执行，
  后续动作基于前序结果继续匹配；任一目标零命中、语法错误或动作非法时整批返回 `422`
  （错误含动作索引，不返回半成品）。成功返回变换后的契约、逐动作实际命中的 JSON Pointer
  与规范 JSON 的 **SHA-256**，命中与报告稳定排序，且不修改请求对象。

- **`POST /requests/materialize`**：提交**一份** OpenAPI 3.x 文档、一个 `METHOD /path`
  操作选择器与按位置分组（`path` / `query` / `header` / `cookie`）的参数值，
  生成可直接发出的 HTTP 请求三件套：**`request_target`**（路径模板替换后拼接查询串）、
  **`headers`** 与 **Cookie 头值**。本地 `$ref`（含参数引用与 OpenAPI 3.1 path-level
  `$ref` 链）循环安全解析；path-level 与 operation-level 参数按 `(in, name)` 合并、
  后者覆盖前者。遵循各位置默认 `style` / `explode`，支持 path 的
  simple/label/matrix、query 的 form/spaceDelimited/pipeDelimited/deepObject、
  header simple 与 cookie form，处理数组、对象、`allowReserved`、重复查询键与
  百分号编码。缺少必填值、未知值、重复参数、值与样式不匹配或悬空 / 循环引用一律
  返回 422，并附来源 JSON Pointer；输出稳定且不修改请求对象。

- **`POST /callbacks/resolve`**：提交**一份** OpenAPI 3.x 文档、一个 `METHOD /path`
  操作选择器与**一次实际请求 / 响应上下文**（`context`：`request` 的 `url` / `method` /
  `path` / `query` / `header` / `body`，`response` 的 `statusCode` / `header` / `body`，
  各字段均可选），服务循环安全地解析所选操作 `callbacks` 中每个 Callback Object 的本地
  `$ref`（含 OpenAPI 3.1 `$ref` 同级字段），把回调键中的**字面文本**与 **`{...}`
  Runtime Expression** 求值拼成实际回调 URL，并保留该 URL 对应的 Path Item Object。
  支持 `$url`、`$method`、`$statusCode`、`$request.path/query/header/body` 与
  `$response.header/body`；body 片段按 **JSON Pointer**（RFC 6901）取值，header 名
  **大小写不敏感**匹配，插值值必须是**标量**。缺值、非法表达式、非标量插值以及
  悬空 / 外部 / 循环引用一律返回 **422**，逐条给出**回调与表达式的来源 JSON Pointer**；
  结果按来源稳定排序，且不修改输入文档与上下文。

## 一条命令启动

需要安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8080/health |
| 审计接口 | `POST http://localhost:8080/audit` |
| 子契约切片接口 | `POST http://localhost:8080/slice` |
| 网关契约合成接口 | `POST http://localhost:8080/compose` |
| 示例校验接口 | `POST http://localhost:8080/examples/validate` |
| 覆盖动作接口 | `POST http://localhost:8080/overlay/apply` |
| 请求物化接口 | `POST http://localhost:8080/requests/materialize` |
| 回调解析接口 | `POST http://localhost:8080/callbacks/resolve` |
| 交互式 API 文档（Swagger UI） | http://localhost:8080/docs |
| OpenAPI 描述文档 | http://localhost:8080/openapi.json |

> 容器内监听 `8080`，由 Compose 映射到宿主机同名端口。如宿主机 8080 被占用，
> 修改 `docker-compose.yml` 中 `"8080:8080"` 的左侧端口即可，例如 `"9090:8080"`。

后台运行与停止：

```bash
docker compose up --build -d   # 后台启动
docker compose down            # 停止并移除容器
```

## 调用示例

```bash
curl -s -X POST http://localhost:8080/audit \
  -H 'Content-Type: application/json' \
  -d '{"baseline": <基线版 OpenAPI JSON>, "candidate": <候选版 OpenAPI JSON>}'
```

请求体：

```json
{
  "baseline": { "openapi": "3.0.3", "paths": { "...": {} } },
  "candidate": { "openapi": "3.0.3", "paths": { "...": {} } }
}
```

成功响应（`200`）：

```json
{
  "compatible": false,
  "breaking_count": 2,
  "info_count": 1,
  "findings": [
    {
      "pointer": "/components/schemas/NewPet",
      "category": "required-added",
      "severity": "breaking",
      "reason": "property 'tag' became required in the request"
    },
    {
      "pointer": "/paths/~1pets/get/parameters",
      "category": "parameter-required-added",
      "severity": "breaking",
      "reason": "query parameter 'limit' became required; previously requests could omit it"
    },
    {
      "pointer": "/paths/~1pets",
      "category": "operation-added",
      "severity": "info",
      "reason": "new operation PUT /pets (compatible)"
    }
  ]
}
```

- `severity = "breaking"`：违反兼容性策略（请求收紧 / 响应放宽）。
- `severity = "info"`：安全方向的变更（新增操作、新增可选参数、请求枚举放宽等），仅作提示。
- `findings` 按 `(pointer, category, reason)` 升序稳定排序。

错误响应：

- `400 invalid_openapi_document`：缺少 `openapi: 3.x` 版本字段或 `paths` 对象。
- `422 unresolvable_reference`：存在无法解析的 `$ref`，错误体逐条给出文档归属、引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "one or more $ref entries could not be resolved; fix the references before the contract can be audited",
  "references": [
    {
      "document": "baseline",
      "ref": "#/components/schemas/DoesNotExist",
      "error": "unresolvable reference '#/components/schemas/DoesNotExist': object has no property 'DoesNotExist'"
    }
  ]
}
```

## `POST /slice`：抽取最小操作子契约

给定一份 OpenAPI 3.x 文档和一组不重复的 `METHOD /path` 选择器，服务生成只描述这些操作的最小子契约：

- 根节点只保留 **`openapi`** 与 **`info`**；其余根字段（`servers`、`tags`、`webhooks` 等）不进入子契约。
- `paths` 下只保留所选路径；**同一路径只保留被选中的方法与 path-level 字段**
  （共享的 `parameters`、`servers`、`summary`、`description`、`x-` 扩展等），
  其他 HTTP 方法一律剔除，不会混入。OpenAPI 3.1 的 path-level `$ref`（可带同级字段）会被解析并内联。
- 操作未声明 `security` 时**继承根级 `security`**（继承结果物化到操作上；显式 `security: []` 表示不鉴权，保持不变），
  并且 `components/securitySchemes` 只保留实际被引用到的方案；一个都没用到时整个 `components` 可以不出现。
- 从所有保留节点递归计算**本地 `$ref` 闭包**（含 `parameters` / `requestBody` / `responses` / schema 等任意位置，
  含 OpenAPI 3.1 `$ref` 的同级字段），并按**原 JSON Pointer** 在子契约中重建依赖，
  因此所有保留的引用仍然可解析。
- **循环引用安全**：循环目标只复制一次，不无限展开（包括 `Pet.parent -> Pet` 形式与纯 `A -> B -> A` 引用链）。
- **只有从保留节点可达的引用会被校验**：可达的悬空指针或非本地引用（`http(s)://`、文件路径等）返回 `422`；
  未选操作 / 未使用组件中的坏引用与本次切片无关，不会阻塞。
- 输出子契约的**对象键序稳定**（统一按键名排序的紧凑 JSON），并给出其 **SHA-256**；
  输入对象的键顺序与选择器顺序都不会影响子契约内容或摘要，同一选择器集合始终得到同一摘要。

### 请求

```bash
curl -s -X POST http://localhost:8080/slice \
  -H 'Content-Type: application/json' \
  -d '{
    "document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"}, "paths": {"...": {}} },
    "selectors": ["GET /pets", "POST /pets/{petId}"]
  }'
```

- `document`：一份 OpenAPI 3.x JSON 文档（对象）。
- `selectors`：**非空**字符串数组，每项形如 `METHOD /path`；方法大小写不敏感（统一按小写匹配/输出），
  路径必须以 `/` 开头并与文档中的路径键逐字一致（路径参数写作 `GET /pets/{petId}`）。

### 成功响应（`200`）

```json
{
  "contract": {
    "openapi": "3.0.3",
    "info": {"title": "Pet API", "version": "1.0"},
    "paths": {
      "/pets": {
        "get": {
          "responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/Pets"}}}, "description": "ok"}},
          "security": [{"oauth": ["pets:read"]}]
        }
      }
    },
    "components": {
      "schemas": {
        "Pet": {"properties": {"id": {"$ref": "#/components/schemas/Id"},
                 "parent": {"$ref": "#/components/schemas/Pet"}}, "type": "object"},
        "Id": {"type": "integer"},
        "Pets": {"items": {"$ref": "#/components/schemas/Pet"}, "type": "array"}
      },
      "securitySchemes": {"oauth": {"flows": {}, "type": "oauth2"}}
    }
  },
  "sha256": "9f8c9a…（64 位十六进制）"
}
```

- `sha256` 是 `contract` 的规范化 JSON 编码（键名升序、无多余空白、UTF-8）的 SHA-256 十六进制摘要。
- 响应中的 `contract` 本身也按稳定键序返回，可直接重新序列化后自行复算摘要进行校验。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `400 invalid_selector`：选择器问题，`messages` 逐条给出原因（顺序稳定，不随入参顺序变化）：
  - 选择器不是非空数组，或单项不满足 `METHOD /path` 形态（如 `"GET/pets"`、`"garbage"`）；
  - **重复选择器**（`GET /pets` 与 `get /pets` 视为同一个）；
  - **未知操作**：路径未声明，或该路径上不存在该方法（path-level `$ref` 目标提供的方法同样认可）。
- `422 unresolvable_reference`：所选操作（或其闭包依赖）中存在无法解析的引用，`references` 逐条给出引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "a $ref reachable from the selected operations could not be resolved; fix the reference or remove the operation that uses it",
  "references": [
    {
      "ref": "#/components/schemas/Missing",
      "error": "unresolvable reference '#/components/schemas/Missing': object has no property 'Missing'"
    }
  ]
}
```

> 400 与 422 的边界：选择器重复 / 指向不存在的方法属于请求错误（400）；
> 选择的方法确实存在、但其保留节点引用了悬空或非本地目标属于契约引用错误（422）。
> 未被选中的操作和组件即使含坏引用也不会被访问，因而不影响切片结果。

## `POST /compose`：合成多服务网关契约

将 **2～20 份** OpenAPI 3.x 源文档各自挂载到指定的路径前缀下，合成一份网关契约，
并返回覆盖每个输出路径、操作与组件的**来源映射**（provenance）。

### 合成规则

- **路径前缀规范化**：自动补前导 `/`、去除末尾 `/`；`""` 与 `"/"` 均表示挂载到网关根路径。
  前缀中不允许出现空白符、`?`、`#`。规范化后的前缀拼接到该源的每条路径前。
- **路径合并与冲突**：不同源贡献到同一合成路径的不同方法会合并进同一个 path item
  （path-level `parameters` 取并集去重，其余 path-level 字段按请求顺序**先写优先**）；
  **同一合成路径 + 同一方法**（方法名大小写不敏感）来自两个源时返回 **409**，并列出冲突双方来源。
  冲突判定会**循环安全地逐层解析 OpenAPI 3.1 path-level `$ref` 链**（目标与同级字段合并、同级优先），
  因此经 `components/pathItems` 引用提供的方法与直接声明的方法同等参与判定；
  409 的 `conflicts.sources` 分别指向各源文档中该操作的 JSON Pointer
  （引用提供的方法指向链终点组件内的操作位置）。
  多个源在同一合成路径上都使用 path-level `$ref` 时，合成 path item **保留先到的引用**，
  其余引用链提供的有效方法（同样按引用目标与同级字段的覆盖关系计算）会作为**同级操作内联**
  进合成 path item（OpenAPI 3.1 中 `$ref` 同级字段优先于引用目标），
  因此所有非冲突方法都能从合成路径解析，且内联操作已完成 security 物化与命名空间前缀化。
- **security 物化**：各源的根级 `security` 会物化到该源每个未声明 `security` 的操作上
  （显式 `security: []` 保持不变）；合成契约不再携带根级 `security`。
- **命名空间前缀**：组件名（`components` 下各 section 的键）、security requirement 的键、
  `operationId`、tag（操作级 `tags` 与根级 `tags` 声明）统一加上 `<service_id>_` 前缀，
  因此跨服务同名组件不会冲突。`service_id` 只能包含字母、数字、`.`、`_`、`-`（须以字母或数字开头），
  以保证前缀化后的组件键仍是合法的 OpenAPI 组件名。
- **`$ref` 改写**：所有指向 `components` 的本地引用（`#/components/<section>/<name>[/...]`，
  支持指向组件内部的深层指针）同步改写为前缀化后的名字；OpenAPI 3.1 的 path-level `$ref`
  （指向 `components/pathItems`）同样改写并保持引用形态，目标组件内的操作照常物化 security、加前缀。
- **引用限制（422）**：外部引用（`http(s)://`、文件路径等）、指向 `components` 以外位置的本地引用
  （如 `#/paths/...`、`#`、`#/info`）、悬空引用均返回 **422**，错误体逐条给出
  `service_id`、源文档中的 **JSON Pointer**、引用串与原因；path-level `$ref` 的**循环引用链**
  （含超过 100 层的异常链）与无法解析为对象的 path item 引用同样按此格式拒绝。
  只校验进入合成契约的内容
  （`paths` / `components` / `tags`）：被丢弃的根字段（`servers`、`webhooks`、`externalDocs`、
  `x-` 扩展等）中的坏引用不会阻塞合成。
- **其余根字段**：`openapi` 取各源最高版本；`info` 使用请求中给定的合成版本；
  根级 `tags` 合并（按前缀化后的名字去重，先写优先）。
- **错误优先级**：请求错误（400）→ 引用错误（422）→ 操作冲突（409）。

### 请求

```bash
curl -s -X POST http://localhost:8080/compose \
  -H 'Content-Type: application/json' \
  -d '{
    "info": {"title": "Gateway", "version": "1.0"},
    "services": [
      {"service_id": "pets", "path_prefix": "/pets", "document": { "openapi": "3.0.3", "...": "..." }},
      {"service_id": "store", "path_prefix": "/store", "document": { "openapi": "3.1.0", "...": "..." }}
    ]
  }'
```

- `info`：合成契约的 info 对象（原样使用）。
- `services`：**2～20** 个源服务，每项含 `service_id`（唯一）、`path_prefix`、`document`（OpenAPI 3.x 文档）。

### 成功响应（`200`）

```json
{
  "contract": {
    "openapi": "3.1.0",
    "info": {"title": "Gateway", "version": "1.0"},
    "tags": [{"name": "pets_animals", "description": "Animal operations"}],
    "paths": {
      "/pets/animals": {
        "get": {
          "operationId": "pets_listAnimals",
          "tags": ["pets_animals"],
          "security": [{"pets_oauth": ["animals:read"]}],
          "responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/pets_Animal"}}}, "description": "ok"}}
        }
      },
      "/store/items": {"get": {"operationId": "store_listItems", "responses": {"...": "..."}}}
    },
    "components": {
      "schemas": {"pets_Animal": {"...": "..."}, "store_Item": {"...": "..."}},
      "securitySchemes": {"pets_oauth": {"flows": {}, "type": "oauth2"}}
    }
  },
  "provenance": {
    "paths": {
      "/pets/animals": [
        {"service_id": "pets", "source_path": "/animals", "source_pointer": "/paths/~1animals"}
      ]
    },
    "operations": {
      "/pets/animals": {
        "get": {"service_id": "pets", "source_path": "/animals",
                "source_pointer": "/paths/~1animals/get"}
      }
    },
    "components": {
      "schemas": {
        "pets_Animal": {"service_id": "pets", "source_pointer": "/components/schemas/Animal"}
      }
    }
  }
}
```

- `provenance.paths`：每个合成路径 → 贡献它的源服务列表（含源路径与源 JSON Pointer）。
- `provenance.operations`：每个合成路径的每个方法 → 源操作位置。
- `provenance.components`：每个组件 section 的每个前缀化名字 → 源组件位置。

### 错误响应

- `400 invalid_compose_request`：服务数量不在 2～20、`service_id` 缺失 / 重复 / 含非法字符、
  `path_prefix` 含空白或 `?`/`#`、文档不是 OpenAPI 3.x、路径键未以 `/` 开头等；`messages` 逐条给出原因。
- `409 operation_conflict`：两个源声明了同一合成路径的同一方法；`conflicts` 逐条给出
  `path`、`method` 与冲突**双方**的来源（`service_id` + 源 JSON Pointer）：

```json
{
  "error": "operation_conflict",
  "message": "multiple services declare the same composed path and HTTP method; ...",
  "conflicts": [
    {
      "path": "/pets/animals",
      "method": "get",
      "sources": [
        {"service_id": "alpha", "source_path": "/animals", "source_pointer": "/paths/~1animals/get"},
        {"service_id": "beta", "source_path": "/animals", "source_pointer": "/paths/~1animals/get"}
      ]
    }
  ]
}
```

- `422 unsupported_reference`：保留内容中存在外部引用、指向 `components` 以外的引用或悬空引用；
  `references` 逐条给出 `service_id`、源文档中的 JSON Pointer、引用串与原因：

```json
{
  "error": "unsupported_reference",
  "message": "one or more retained $ref entries are external, unresolvable, or point outside components; ...",
  "references": [
    {
      "service_id": "pets",
      "pointer": "/paths/~1animals/get/responses/200/content/application~1json/schema",
      "ref": "https://example.com/schemas.json#/Animal",
      "error": "unsupported non-local reference 'https://example.com/schemas.json#/Animal': ..."
    }
  ]
}
```

> 与 `/audit`、`/slice` 一致：保留内容中任何位置的 `"$ref"` 字符串（包括示例值内部）都按引用处理。
> 混合 3.0 / 3.1 源时合成契约取最高版本，但 schema 方言差异（如 `nullable`）由调用方自行保证兼容。

## `POST /examples/validate`：校验请求 / 响应示例

给定**一份** OpenAPI 3.x 文档，服务遍历每个操作的 `requestBody` 与各响应，
对其中 **JSON 媒体类型**（精确匹配 `application/json`，或任意 `*+json` 结构媒体类型；
`text/plain`、`application/octet-stream` 等不检查）携带的示例逐一按该媒体类型的 `schema` 校验：

- Media Type Object 上的内联 `example`；
- `examples` 映射中的每个 Example Object，包括经本地 `$ref`
  （`#/components/examples/...`、`#/components/requestBodies/...`、
  `#/components/responses/...`、3.1 `components/pathItems/...`）解析到的对象；
  校验所用的 **Schema `$ref` 同样解析**（含递归 Schema，循环安全）。

### 方向性语义（OpenAPI 规范）

- **请求**示例中出现 Schema 标记为 `readOnly: true` 的属性 → 报错（`keyword: "readOnly"`）；
  **响应**示例中出现 `writeOnly: true` 的属性 → 报错（`keyword: "writeOnly"`）。
- `required` **只在对应方向生效**：`readOnly` 属性即使列入 `required`，也只要求响应必带
  （请求示例缺失不报错）；`writeOnly` 属性相反，只要求请求必带。
- **跨 `allOf` 分支的方向标记同样生效**：校验前会循环安全地汇总同一 `allOf` 对象各分支
  （含分支自身或属性经本地 `$ref` 到达、以及嵌套 `allOf`）的有效 `properties` 注解——
  一支声明 `required: [id]`、另一支把 `id` 标为 `readOnly: true` 时，请求示例省略 `id`
  合法；响应侧跨分支的 `writeOnly` 同理豁免。汇总只用于方向性 `required` 豁免，
  各分支的其他约束（`type`、`minProperties` 等）仍分别强制执行，`anyOf` / `oneOf`
  备选分支之间不共享这一视图。
- **类型可空性按版本区分**：OpenAPI 3.0 用 `nullable: true` 放行 `null`；
  OpenAPI 3.1（JSON Schema 2020-12 方言）用类型联合 `"type": ["string", "null"]`，
  此时 `nullable` 关键字被忽略。`integer` 是 `number` 的子集；无小数部分的数字也满足 `integer`。

### 支持的 Schema 关键字

`type` / `enum` / `const` / `required` / `properties` / `additionalProperties` /
`minProperties` / `maxProperties` / `items`（含 3.1 位置元组形态）/ `minItems` / `maxItems` /
`uniqueItems` / `minLength` / `maxLength` / `pattern` / `minimum` / `maximum` /
`exclusiveMinimum` / `exclusiveMaximum`（3.0 布尔修饰符与 3.1 数值边界两种写法均支持）/
`multipleOf` / `allOf` / `anyOf` / `oneOf`，以及布尔 Schema（3.1）。`$ref` 可出现在任意位置。

### 请求

```bash
curl -s -X POST http://localhost:8080/examples/validate \
  -H 'Content-Type: application/json' \
  -d '{"document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"}, "paths": {"...": {}} }}'
```

### 成功响应（`200`，即使校验失败也是 200）

```json
{
  "valid": false,
  "error_count": 2,
  "errors": [
    {
      "side": "request",
      "example_pointer": "/paths/~1pets/post/requestBody/content/application~1json/example",
      "instance_pointer": "/created",
      "schema_pointer": "/components/schemas/Pet/properties/created",
      "keyword": "readOnly",
      "reason": "readOnly property 'created' must not appear in a request example"
    },
    {
      "side": "response",
      "example_pointer": "/paths/~1pets/post/responses/200/content/application~1json/examples/byId/value",
      "instance_pointer": "/id",
      "schema_pointer": "/components/schemas/Pet/properties/id",
      "keyword": "type",
      "reason": "expected type integer, got string"
    }
  ]
}
```

字段说明：

- `side`：示例方向，`request`（来自 `requestBody`）或 `response`（来自响应）。
- `example_pointer`：示例值在文档中的 JSON Pointer。经 `$ref` 引用的 Example Object
  会**跟随到组件定义位置**（如 `/components/examples/byId/value`），与 `/audit` 的 Pointer 约定一致；
  同一组件示例被多处复用时，相同违规只报告一次。
- `instance_pointer`：违规值在示例 JSON 内部的 JSON Pointer（根示例为 `""`，深层如 `/tags/0/name`）。
- `schema_pointer`：被违反的 Schema 位置（本地 `$ref` 跟随到组件定义）。
- `keyword` / `reason`：违反的 Schema 关键字与人类可读原因。
- `errors` 按 **`(example_pointer, instance_pointer, schema_pointer, keyword, reason)` 升序稳定排序**，
  与输入键序无关。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `422 unsupported_reference`：示例使用了 `externalValue`（服务不会发起外部网络请求），
  或示例 / Schema 中出现外部 `$ref`（`http(s)://`、文件路径等）。
- `422 unresolvable_reference`：校验过程中可达的本地 `$ref` 悬空（包括示例组件间的纯 `$ref` 循环链）。
- `422 invalid_example`：示例 `$ref` 目标不是 Example Object。

422 响应体的 `references` 逐条给出**来源 JSON Pointer**（引用所在位置）、引用串与原因：

```json
{
  "error": "unresolvable_reference",
  "message": "one or more $ref entries reached while validating examples could not be resolved; fix the references first",
  "references": [
    {
      "pointer": "/paths/~1pets/get/responses/200/content/application~1json/schema",
      "ref": "#/components/schemas/Missing",
      "error": "unresolvable reference '#/components/schemas/Missing': object has no property 'Missing'"
    }
  ]
}
```

> 只有从被检查示例可达的引用才会触发 422；未被任何示例使用的组件里即使有坏引用也不影响结果。
> 媒体类型未声明 `schema` 时，示例不受任何约束（但 `externalValue` / 坏引用仍按上述规则返回 422）。
> Example Object 既无 `value` 也无 `externalValue` 时无可校验内容，跳过不报。

## `POST /overlay/apply`：对契约应用有序覆盖动作

给定**一份** OpenAPI 3.x 文档和 **1～50 个有序动作**，服务按顺序把每个动作应用到文档上，
返回变换后的契约、逐动作实际命中的 JSON Pointer 以及规范 JSON 的 SHA-256。
整个批次是**原子**的：任何一个动作非法、目标语法错误或零命中，整批以 `422` 拒绝，
不返回半成品，也绝不修改请求中的文档对象。

### 动作格式

每个动作是一个 JSON 对象，只能包含 `target`、`update`、`remove` 三个键：

- `target`（必填，非空字符串）：受限 JSONPath（见下）。
- `update`：任意 JSON 值。与 `remove` **二者必选其一**，不能同时出现或同时缺失。
- `remove`：必须为 `true`（`false`、其他值或与其他键组合均为非法动作）。

```json
{"target": "$.info", "update": {"contact": {"name": "team"}}}
{"target": "$.paths['/internal']", "remove": true}
```

### 受限 JSONPath 目标语法

只支持以下五种段（segment），**不允许任何空白符**：

| 语法 | 含义 |
| --- | --- |
| `$` | 文档根节点 |
| `.name` | 对象成员，名字限 `[A-Za-z0-9_]+`（如 `$.paths`、`$.responses.200`） |
| `['name']` / `["name"]` | 任意成员名（可含 `/`、`-`、空格等），支持反斜杠转义（`\\`、`\'`、`\"`、`\/`、`\b`、`\f`、`\n`、`\r`、`\t`、`\uXXXX`） |
| `[0]` | 数组下标（非负整数，不允许负下标与前导零） |
| `.*` / `[*]` | 通配符：对象的全部成员值或数组的全部元素（对标量不命中） |

**不支持**过滤器（`$[?(...)]`）与递归下降（`$..name`），出现即按语法错误拒绝（422）。
成员段作用于非对象、下标段作用于非数组、通配符作用于标量时均不命中。

### 应用语义

- **`update`**：目标值与更新值**都是对象**时**递归合并**（逐成员深入，子对象继续合并）；
  其余组合一律**整体替换**目标值（数组不逐元素合并，而是整体替换；`update: null` 会把目标置为 `null`）。
  同一动作的更新值对每个命中节点分别深拷贝应用，命中之间互不影响。
- **`remove`**：删除全部命中节点；**禁止删除根节点** `$`（非法动作，422）。
- **冻结与顺序**：同一动作先对当前文档状态**冻结全部命中**，再统一应用；
  数组元素的删除按**索引倒序**执行，避免前面的删除改变后面待删元素的位置。
  后续动作基于前序动作的结果继续匹配（例如先 `remove` 一个数组元素，后续动作看到的下标已前移）。
- **零命中**：任一动作的目标匹配到 0 个节点（成员不存在、下标越界、通配符作用于标量等），
  整批返回 `422 no_match`。

### 请求

```bash
curl -s -X POST http://localhost:8080/overlay/apply \
  -H 'Content-Type: application/json' \
  -d '{
    "document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"},
                  "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
                  "servers": [{"url": "https://old.example.com"}] },
    "actions": [
      {"target": "$.servers[0]", "update": {"url": "https://api.example.com"}},
      {"target": "$.paths.*.get.responses.200", "update": {"content": {"application/json": {}}}}
    ]
  }'
```

- `document`：一份 OpenAPI 3.x JSON 文档（对象）。
- `actions`：**1～50** 个动作对象，按数组顺序依次应用。

### 成功响应（`200`）

```json
{
  "contract": { "openapi": "3.0.3", "info": {"...": "..."}, "paths": {"...": "..."} },
  "actions": [
    {"action_index": 0, "op": "update", "target": "$.servers[0]", "matches": ["/servers/0"]},
    {"action_index": 1, "op": "update", "target": "$.paths.*.get.responses.200",
     "matches": ["/paths/~1pets/get/responses/200"]}
  ],
  "sha256": "…（64 位十六进制）"
}
```

- `contract`：变换后的文档，按稳定键序（键名升序）返回。
- `actions`：逐动作报告，按请求中的动作顺序排列；`matches` 是该动作**实际命中**的
  JSON Pointer（RFC 6901 转义，如 `/paths/~1pets`），按 Pointer 字符串升序稳定排序。
- `sha256`：`contract` 的规范化 JSON 编码（键名升序、无多余空白、UTF-8）的 SHA-256；
  响应中的 `contract` 本身即稳定键序，可直接重新序列化复算摘要校验。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `400 invalid_overlay_request`：动作数量不在 1～50 范围内。
- `422 invalid_action`：动作非法或目标语法错误，`actions` 逐条给出
  `action_index`（动作下标，从 0 开始）、`target`（如可得）与原因，**全部非法动作一次报齐**：

```json
{
  "error": "invalid_action",
  "message": "one or more overlay actions are malformed or carry an invalid target; the whole batch was rejected and the document was left untouched",
  "actions": [
    {"action_index": 1, "target": "$..info",
     "error": "invalid target '$..info': expected a member name or '*' after '.' (position 2)"},
    {"action_index": 3, "target": "$",
     "error": "the root node '$' cannot be removed"}
  ]
}
```

  非法情形包括：动作不是对象、含未知字段、`target` 缺失或非字符串、`update` 与 `remove`
  同时出现或同时缺失、`remove` 不是 `true`、删除根节点 `$`、目标语法错误
  （过滤器、递归下降、负下标、前导零、未闭合的括号/引号、多余空白等）。
- `422 no_match`：某动作目标零命中（按动作顺序报告第一个），`actions` 给出其
  `action_index` 与 `target`：

```json
{
  "error": "no_match",
  "message": "an action target matched zero nodes; the whole batch was rejected and the document was left untouched",
  "actions": [
    {"action_index": 2, "target": "$.paths['/missing']",
     "error": "target '$.paths['/missing']' matched 0 nodes"}
  ]
}
```

> 所有 422 都是整批拒绝：响应不含 `contract`，请求中的文档对象也不会被改动。
> `actions` 不是数组时由 FastAPI 以默认 422 格式返回。

## `POST /requests/materialize`：物化操作的请求目标、headers 与 Cookie

给定**一份** OpenAPI 3.x 文档、一个操作选择器与一组参数值，服务解析该操作的有效参数
（循环安全），按 OpenAPI 3 参数序列化规则生成实际 HTTP 请求的三个部分：

- **`request_target`**：origin-form 请求目标——路径模板中的 `{name}` 被替换为序列化后的
  path 参数，存在 query 参数时再拼接 `?` 与查询串；
- **`headers`**：每个 `in: header` 参数一个键值对（键为契约中声明的头名，值为序列化结果）；
- **`cookie`**：由全部 `in: cookie` 参数组成的 `Cookie` 头值（`name=value` 以 `; ` 连接）；
  Cookie 不会重复出现在 `headers` 中。

### 参数合并与引用解析

- **path-level 与 operation-level 合并**：先按文档顺序排列 path-level 参数，再追加仅在
  operation-level 声明的参数；operation-level 参数与 path-level 参数具有相同
  **`(in, name)`** 时**整体覆盖**前者（OpenAPI 3 规范）。
- **同级重复参数**（同一 parameters 数组中两个相同 `(in, name)`，或两个仅大小写不同的
  header 参数）为 422，错误同时给出两处来源 Pointer。
- 参数与 path item 上的本地 `$ref` 均被解析：参数 `$ref` 跟随到组件定义，报错时
  定义类问题（缺值、样式不合法等）指向**组件位置**、引用类问题（悬空、循环）指向
  **`$ref` 所在位置**。OpenAPI 3.1 下 `$ref` 的同级字段逐层合并（近者优先）；
  3.0 文档严格遵循旧规范、忽略同级字段。path-level `$ref` 链（含 3.1
  `components/pathItems`）同样循环安全，超深链按上限拒绝。

### 默认样式与支持矩阵

未声明 `style` / `explode` 时采用各位置默认值：

| 位置 | 支持的 style | 默认 style | 默认 explode |
| --- | --- | --- | --- |
| path | `simple` / `label` / `matrix` | `simple` | `false` |
| query | `form` / `spaceDelimited` / `pipeDelimited` / `deepObject` | `form` | `true` |
| header | `simple` | `simple` | `false` |
| cookie | `form` | `form` | （cookie 表忽略 explode） |

序列化与 OpenAPI 3.0.3 规范示例表一致，例如：

- path `simple`：标量 `id`；数组 `a,b,c`；对象 explode 为 `k=v,k2=v2`、非 explode 为
  `k,v,k2,v2`。`label` 带 `.` 前缀（数组 explode 用 `.` 连接）；`matrix` 为
  `;id=...`（数组 explode 重复 `;id=`，对象 explode 直接用属性名）。
- query `form`：数组 explode 产生**重复查询键** `id=a&id=b`，非 explode 为 `id=a,b`；
  对象 explode 用属性名作为查询键。`spaceDelimited` / `pipeDelimited` 仅接受数组
  （非 explode 分别用 `%20`、`|` 连接；explode 仍重复查询键）。`deepObject` 仅接受对象，
  输出 `id[role]=admin`，并始终是 exploded 形态。
- query 的 **`allowReserved: true`** 允许 RFC 3986 保留字符（`:/?#[]@!$&'()*+,;=`）
  原样出现在值中；默认情况下所有非 unreserved 字节都按 UTF-8 **百分号编码**
  （空格为 `%20`、`€` 为 `%E2%82%AC`）。序列化结构分隔符（`&`、`=`、`,`、`|`、`%20`、
  方括号）本身保持原样。
- header `simple` 不做百分号编码（逗号 / 等号为结构分隔符）；值中含 CR / LF / NUL
  时返回 422；请求里的头名按**大小写不敏感**匹配，输出使用契约声明的名字。
- cookie `form` 按 cookie 表序列化：数组 `a,b`、对象 `k,v,k2,v2`（explode 被忽略），
  各 token 按 unreserved 集合百分号编码，多个 cookie 以 `; ` 连接。
- 布尔值渲染为 `true` / `false`；数字按 JSON 数值文本渲染；数组元素与对象属性值必须是
  标量（字符串 / 数字 / 布尔），嵌套数组、对象或 `null` 元素返回 422。对象成员按
  **键名升序**输出，因此结果与输入键序无关。

### 请求

```bash
curl -s -X POST http://localhost:8080/requests/materialize \
  -H 'Content-Type: application/json' \
  -d '{
    "document": { "openapi": "3.0.3", "info": {"title": "Pet API", "version": "1.0"},
                  "paths": {"/pets/{petId}": {"...": "..."}} },
    "selector": "GET /pets/{petId}",
    "values": {
      "path":   {"petId": "cat/42"},
      "query":  {"tags": ["cute", "small"], "filter": {"status": "available"}},
      "header": {"X-Tenant": "demo"},
      "cookie": {"session": "abc"}
    }
  }'
```

- `document`：一份 OpenAPI 3.x JSON 文档（对象）。
- `selector`：形如 `METHOD /path` 的单个选择器（方法大小写不敏感，路径须逐字匹配）。
- `values`（可省略）：按位置分组的参数值；header 名大小写不敏感匹配。
  显式传 `null` 等同未提供该值——必填参数因此报错，可选参数则从请求中省略。

### 成功响应（`200`）

```json
{
  "request_target": "/pets/cat%2F42?tags=cute&tags=small&status=available",
  "headers": {"X-Tenant": "demo"},
  "cookie": "session=abc"
}
```

> 上例中 `filter` 是默认 form+explode 的对象，属性名直接成为查询键（`status=available`）；
> 若契约声明 `style: deepObject`，对应片段为 `filter[status]=available`（方括号保持原样）。
> 重复查询键按参数声明顺序排列；query 参数整体也按声明顺序输出。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `400 invalid_selector`：选择器不是 `METHOD /path` 形态，或路径 / 方法不存在。
- `400 invalid_materialize_request`：`values` 不是对象、含未知分组（只允许
  `path` / `query` / `header` / `cookie`）或某个分组不是对象。
- `422 materialization_failed`：所有语义问题一次报齐，`errors` 按
  `(code, in, name, pointer, error)` 稳定排序，每条至少带一个**来源 JSON Pointer**：

| code | 触发情形 | 典型 pointer |
| --- | --- | --- |
| `missing_required_value` | 必填参数（path 参数恒必填）未提供值（含显式 `null`） | 参数定义位置（`$ref` 跟随到组件） |
| `unknown_value` | 为操作未声明的参数提供了值 | `/values/<in>/<name>` |
| `ambiguous_value` | header 分组里出现仅大小写不同的两个键 | `/values/header/<name>` |
| `duplicate_parameter` | 同级参数数组重复 `(in, name)`，或 header 参数仅大小写冲突 | 后一处定义位置，附 `first_pointer` |
| `invalid_value_shape` | 值与样式不匹配（deepObject 非对象、space/pipe 非数组、标量样式收到结构值等） | `/values/<in>/<name>` |
| `unsupported_style` | 样式不属于该位置的支持矩阵 | 参数的 `/style` 位置 |
| `unsupported_parameter` | 参数使用 `content` 媒体类型对象（不支持物化） | 参数定义位置 |
| `invalid_parameter` | 缺少字符串 `name`、`in` 非法、参数 / path item 不是对象、头名不是合法 token 等 | 问题节点位置 |
| `invalid_path_template` | 模板变量没有对应参数、path 参数未在模板中使用、模板花括号非法 | path item 或参数位置 |
| `unresolvable_reference` | 参数 / path item 的本地 `$ref` 悬空、非本地或循环 | `$ref` 所在位置，附 `ref` |

```json
{
  "error": "materialization_failed",
  "message": "the request could not be materialized; fix the reported parameters, values or references first",
  "errors": [
    {
      "code": "missing_required_value",
      "pointer": "/paths/~1pets~1{petId}/get/parameters/0",
      "in": "path",
      "name": "petId",
      "error": "missing value for required path parameter 'petId'"
    },
    {
      "code": "unresolvable_reference",
      "pointer": "/paths/~1pets/get/parameters/2",
      "ref": "#/components/parameters/Missing",
      "error": "unresolvable reference '#/components/parameters/Missing': object has no property 'Missing'"
    }
  ]
}
```

> 与其他接口一致：物化过程**不会修改**请求中的 `document` / `values` 对象；
> 对象类型的值按键名升序序列化、数组按声明顺序序列化，同样的契约与值无论输入键序如何，
> 始终得到字节相同的 `request_target` / `headers` / `cookie`。

## `POST /callbacks/resolve`：解析操作声明的异步回调 URL

用于**调试契约声明的异步回调**：给定一份 OpenAPI 3.x 文档、一个操作选择器和一次实际
请求 / 响应上下文，服务定位所选操作的 `callbacks`，逐个解析 Callback Object（循环安全），
把每个回调键求值为具体 URL，并连同该键声明的 **Path Item Object** 一起返回。

### 回调键（Runtime Expression）求值规则

Callback Object 的每个键要么是**固定 URL 字面量**（如 `https://hooks.example.com/ping`），
要么按 OpenAPI 3 Runtime Expression 求值：

- **完整表达式单独成键**：键整体是一个合法表达式，例如 `$url`、`$request.body#/callbackUrl`。
- **模板内嵌表达式**：键内出现一个或多个 `{...}` 片段，花括号内是完整表达式，
  其余文字逐字保留，例如 `https://hooks.example.com/{$request.path.txId}?status={$statusCode}`。
- 支持的表达式：
  - `$url`、`$method`：本次请求的 URL 与 HTTP 方法；
  - `$statusCode`：响应状态码（数字按 JSON 数值文本渲染）；
  - `$request.path.<name>` / `$request.query.<name>`：请求路径 / 查询参数；`<name>`
    遵循规范的 `name` 产生式，支持 `%HH` 百分号转义（求值前解码后再匹配上下文键）；
  - `$request.header.<token>` / `$response.header.<token>`：请求 / 响应 header，
    `<token>` 为 RFC 7230 tchar；匹配**大小写不敏感**（表达式用 `x-trace` 也能命中
    上下文里的 `X-Trace`）；上下文里若有仅大小写不同的两个同名 header，返回 422；
  - `$request.body` / `$response.body`：整个消息体（必须为标量才能插值）；
  - `$request.body#<JSON Pointer>` / `$response.body#<JSON Pointer>`：按 RFC 6901
    JSON Pointer 在 body 中取值（如 `#/items/0/href`、`#/nested/deep/url`；
    `~0`/`~1` 转义、数组下标规则与标准一致）。
- 插值值必须是**标量**（字符串 / 数字 / 布尔）：取到对象、数组（或整体 body 为结构值）
  一律 422；取到 `null`、路径不可达、对应 message（request/response）或字段未提供，
  均视为**缺值** 422。
- 非法表达式（未知根 / 源，如 `$foo`、`$response.path.x`、`{txId}` 裸变量、空 `{}`、
  未闭合或嵌套花括号、非法 JSON Pointer 转义、非法 `%HH`、尾部杂散字符等）返回 422。
  不含花括号、整体又不是合法完整表达式的键按**固定字面量**处理（如 `$url/x` 不会报错）。

### Callback Object 与 Path Item 的 `$ref` 解析

- 操作 `callbacks/<name>` 条目本身可以是本地 `$ref`（指向 `#/components/callbacks/...`
  等），表达式键对应的 **Path Item Object 也可以是本地 `$ref`**；两条链都**循环安全**
  （记录已访问指针，成环即 422，另有 100 层链长上限）。
- **OpenAPI 3.1**：`$ref` 的同级字段逐层合并（近者优先），同级键可覆盖 / 扩展引用目标
  中的同名表达式键；**OpenAPI 3.0**：严格遵循旧规范，`$ref` 同级字段被忽略，以链终点
  对象为准。
- 仅本地引用 `#/...` 受支持：外部引用（`http(s)://`、文件路径等）、悬空指针、引用目标
  非对象均返回 422。Path Item 内部更深层的 `$ref`（如 `requestBody: $ref`）不展开，
  随 Path Item 原样深拷贝返回，因此正常的递归 schema 不会被误判为循环。
- 操作所在的 path-level `$ref`（OpenAPI 3.1 `components/pathItems`）同样循环安全解析后
  再定位操作。

### 来源 JSON Pointer

- `callback_pointer`：回调条目（`callbacks/<name>`）位置；引用型条目指向 **`$ref` 出现处**。
- `expression_pointer`：表达式键在**有效 Callback Object** 中的位置；引用型回调会跟随到
  组件内的键声明（如 `/components/callbacks/OnEvent/$url`），被 3.1 同级字段覆盖的键
  则指向覆盖处。路径键按 RFC 6901 转义（`/` → `~1`、`~` → `~0`）。
- 422 错误中的 `pointer` 为主要修复位置（表达式错误指向表达式键，引用错误指向 `$ref`
  出现处），并同时附带 `callback_pointer`、`expression_pointer`、原始 `expression`、
  `ref` 与上下文中的 `context_pointer`。

### 请求

```bash
curl -s -X POST http://localhost:8080/callbacks/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "document": { "openapi": "3.0.3", "info": {"title": "Hook API", "version": "1.0"},
                  "paths": {"/subscribe": {"post": {"callbacks": {"...": "..."}}}} },
    "selector": "POST /subscribe",
    "context": {
      "request": {
        "url": "https://api.example.com/subscribe", "method": "POST",
        "path": {"txId": "abc-123"}, "query": {"limit": "50"},
        "header": {"X-Trace": "t-9"},
        "body": {"callbackUrl": "https://my.host/hook"}
      },
      "response": {"statusCode": 202, "header": {"X-Request-Id": "r1"}}
    }
  }'
```

- `document`：一份 OpenAPI 3.x JSON 文档（对象）。
- `selector`：形如 `METHOD /path` 的单个选择器（方法大小写不敏感，路径须逐字匹配）。
- `context`（可省略，默认为空对象）：请求 / 响应上下文。`request` 与 `response` 及其
  各字段均可选；`path` / `query` / `header` 为对象，header 值必须是标量字符串，
  `statusCode` 为整数或字符串，`body` 为任意 JSON。

### 成功响应（`200`）

```json
[
  {
    "name": "onEvent",
    "callback_pointer": "/paths/~1subscribe/post/callbacks/onEvent",
    "expression": "https://hooks.example.com/{$request.path.txId}?status={$statusCode}",
    "expression_pointer": "/paths/~1subscribe/post/callbacks/onEvent/https:~1~1hooks.example.com~1{$request.path.txId}?status={$statusCode}",
    "url": "https://hooks.example.com/abc-123?status=202",
    "path_item": {"post": {"responses": {"2XX": {"description": "ack"}}}}
  }
]
```

- 结果数组按 **`(callback_pointer, expression_pointer)` 升序稳定排序**，与回调键的
  JSON 输入顺序无关。
- 操作没有 `callbacks` 时返回空数组 `[]`。
- 每个结果的 `path_item` 是该 URL 对应 Path Item Object 的深拷贝（自身 `$ref` 链已解析、
  3.1 同级已合并；其内部 `$ref` 保持原样），供调试时查看回调请求的方法、请求体与响应。

### 错误响应

- `400 invalid_openapi_document`：`document` 缺少 `openapi: 3.x` 或 `paths` 对象。
- `400 invalid_selector`：选择器不是 `METHOD /path` 形态，或路径 / 方法不存在。
- `400 invalid_callback_request`：`context` 信封非法（未知 section / 字段、
  容器不是对象、`url` / `method` 非字符串、`statusCode` 非整数或字符串、
  header 值非标量等），`messages` 逐条给出原因。
- `422 callback_resolution_failed`：所有语义问题一次报齐并稳定排序，`errors` 每条至少带
  `code`、`pointer`、`error`，并附回调 / 表达式来源 Pointer：

| code | 触发情形 | pointer 指向 |
| --- | --- | --- |
| `missing_value` | 表达式取到 `null`、JSON Pointer 不可达，或对应 request/response/字段未提供 | 表达式键（另附 `context_pointer`） |
| `invalid_expression` | 表达式语法非法（未知源、空 / 嵌套 / 未闭合花括号、非法指针转义等） | 表达式键 |
| `non_scalar_interpolation` | 取到对象或数组，无法插入 URL | 表达式键（另附 `context_pointer`） |
| `ambiguous_header` | header 名大小写不敏感匹配到多个仅大小写不同的上下文键 | 表达式键 |
| `unresolvable_reference` | 回调 / Path Item 的本地 `$ref` 悬空、非本地、目标非对象或循环 | `$ref` 出现处（另附 `ref`） |
| `invalid_callback` | 回调值不是 Callback Object、表达式值不是 Path Item Object、`callbacks` 不是对象等 | 问题节点 |

```json
{
  "error": "callback_resolution_failed",
  "message": "one or more callback URLs could not be resolved; fix the reported expressions, context values or references first",
  "errors": [
    {
      "code": "missing_value",
      "pointer": "/paths/~1subscribe/post/callbacks/onEvent/{$request.body#~1callbackUrl}",
      "callback_pointer": "/paths/~1subscribe/post/callbacks/onEvent",
      "expression_pointer": "/paths/~1subscribe/post/callbacks/onEvent/{$request.body#~1callbackUrl}",
      "expression": "$request.body#/callbackUrl",
      "context_pointer": "/context/request/body/callbackUrl",
      "error": "runtime expression '$request.body#/callbackUrl' has no value in the supplied request/response context"
    }
  ]
}
```

> 解析过程**不会修改**请求中的 `document` 与 `context` 对象；不同回调 / 表达式的全部
> 错误会一次报齐。循环安全仅作用于回调与 Path Item 层的 `$ref` 链，Path Item 内部的
> 递归 schema 引用随 Path Item 原样返回，不会阻塞解析。

## 检查的变更类别

| category | 说明 | 判定为 breaking 的方向 |
| --- | --- | --- |
| `operation-removed` / `operation-added` | 路径上 HTTP 操作被删除 / 新增 | 删除操作 |
| `path-removed` / `path-added` | 整条路径被删除 / 新增 | 删除路径 |
| `parameter-added` / `parameter-removed` | 新增 / 删除参数（含路径级参数） | 新增**必填**参数 |
| `parameter-required-added` / `parameter-required-removed` | 参数必填性变化 | 参数由可选变必填 |
| `request-body-added` / `request-body-required-added` | 新增请求体或请求体变为必填 | 必填请求体被引入 |
| `type-changed` | 类型 / 可空性变化（参数、请求体、响应字段、数组 items），按值集合包含关系判定 | 请求侧收紧（如 `number`→`integer`、去除可空、新增类型约束）；响应侧放宽（如 `integer`→`number`、3.0 `nullable` / 3.1 `"null"` 联合新增可空） |
| `enum-changed` | 枚举值增删；缺省 `enum` 视为**无约束全集**而非空集 | 请求侧收紧（枚举收窄、新引入 enum）；响应侧放宽（枚举放宽、移除 enum 约束） |
| `required-added` / `required-removed` | 对象属性必填性变化 | 请求新增必填字段、响应取消必填保证 |
| `property-added` / `property-removed` | 对象属性增删 | 请求新增必填属性、响应删除属性 |
| `response-removed` / `response-added` | 响应状态码（含 `2XX` 范围码）增删 | 删除响应码 |
| `response-body-removed` | 响应体被移除 | 始终 breaking |
| `media-type-removed` / `media-type-added` | 请求/响应的媒体类型增删 | 删除调用方依赖的媒体类型 |
| `schema-variants-changed` | `oneOf` / `anyOf` 分支数量变化 | 始终 breaking |

### 类型与枚举的集合判定细则

schema 约束按**可接受 / 可产出的值集合**比较，而非关键字字面相等：

- **缺省即无约束**：未声明 `type` 视为接受所有基础类型，未声明 `enum` 视为接受该类型下所有值（均为全集，而非空集）。
  因此请求侧新增 `type` / `enum` 是收紧（breaking），响应侧移除 `type` / `enum` 约束是放宽（breaking）；反方向为 info。
- **integer 与 number 的包含关系**：`integer ⊂ number`。请求侧 `integer → number`（放宽）为 info，`number → integer`（收紧）为 breaking；
  响应侧结论相反（`integer → number` 可能产出小数，为 breaking）。
- **OpenAPI 3.0 `nullable: true`** 等价于在值集合中加入 `null`；与 OpenAPI 3.1 的类型联合 `"type": ["string", "null"]` 互相等价，
  两种写法之间迁移不报告。3.1 文档中的 `nullable` 关键字会被忽略（它不是 JSON Schema 2020-12 的关键字）。
- 当 `enum` 存在且未显式列出 `null` 时，`null` 不在允许集合内（此时 `nullable` / 联合中的 `"null"` 不生效）。

## JSON Pointer 约定

- **删除类**变更指向**基线文档**中的位置；**新增 / 修改类**变更指向**候选文档**中的位置。
- 节点为 `$ref` 时，Pointer 会跟随到最终组件定义，例如
  `#/components/schemas/Pet/properties/status`，这是修复契约问题的稳定位置；同一组件被多处引用时
  问题只需在组件定义处修复一次。
- Pointer 遵循 RFC 6901 转义（`~0` → `~`，`~1` → `/`），例如路径 `/pets` 显示为 `/paths/~1pets`。

## `$ref` 解析与循环引用保护

- 仅支持文档内本地引用（`#/...`，含根引用 `#`）；远程 URL、文件路径等非本地引用一律报为
  `unresolvable_reference`（服务不会发起任何外部网络请求）。
- 引用链记录已访问指针：
  - 正常的**递归 schema**（`Node.children -> #/components/schemas/Node`）会被识别并在成环处停止遍历，
    不会无限展开；
  - 纯 `$ref` 死循环（`A -> B -> A`）抛出明确的 `circular reference detected` 错误；
  - 超过 100 层的异常引用链有硬性上限保护；
- 结构比较阶段以“组件指针 / 节点身份 + 比较方向”做记忆化去重，并带有 300 层内联嵌套深度上限，
  双重保证循环引用不会失控。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

运行测试：

```bash
pytest -q
```

测试覆盖：引用解析（缺失指针、数组越界、远程引用、转义 token、自循环）、操作/参数增删、
类型/枚举/required 双向规则、integer/number 包含、缺省约束、3.0 `nullable` 与 3.1 类型联合、
响应码与媒体类型删除、递归 schema 不爆炸、报告排序；以及 `/slice` 的子契约裁剪、
`$ref` 传递闭包与循环复制一次、未选分支坏引用不阻塞、`security` 继承与 `securitySchemes` 过滤、
3.1 path-level `$ref` 内联、400/422 边界、SHA-256 与键序 / 选择器顺序无关性；
以及 `/compose` 的前缀规范化与根挂载、路径合并与 409 冲突双方来源、security 物化与键前缀、
组件 / `operationId` / tag 命名空间化、`$ref` 改写与 422（外部 / 非 components / 悬空，含源 JSON Pointer）、
path-level `$ref` 链（含多级与中间层同级方法）的冲突检测与来源指针、
同一合成路径上多条 `$ref` 链的合并（非冲突方法内联后均可从输出路径解析、有效方法按同级覆盖取舍）、
循环 / 超深引用链 422、
来源映射完整性、2～20 数量边界、重复 `service_id` 400、被丢弃根字段中的坏引用不阻塞；
以及 `/examples/validate` 的内联 `example` 与 `examples.<name>.value`（含本地 `$ref` 示例组件）校验、
`readOnly` / `writeOnly` 方向报错、`required` 仅对应方向生效、跨 `allOf` 分支（内联与
属性 / 分支本地 `$ref`、嵌套 `allOf`、循环 `allOf`）的方向标记豁免、请求与响应双向回归、
`anyOf` / `oneOf` 备选间不串用分支视图、物理 Schema Pointer 与稳定排序保持不变、
3.0 `nullable` 与 3.1 类型联合、
integer/number 与布尔值区分、enum/const/allOf/anyOf/oneOf/数值·字符串·数组·对象约束关键字、
递归 Schema 不爆炸、按示例与实例 Pointer 稳定排序、同一示例组件复用去重、
`externalValue` / 外部引用 / 悬空引用 / 非对象示例引用 422（含来源 Pointer）、
不可达坏引用不阻塞、非 JSON 媒体类型跳过、400 文档形态错误；
以及 `/overlay/apply` 的受限 JSONPath 解析（点成员 / 引号成员与转义 / 下标 / 通配符、
过滤器与递归下降 422）、`update` 递归合并与整体替换、`remove` 根节点禁止、
同一动作命中冻结与数组倒序删除、后续动作基于前序结果匹配、逐命中独立深拷贝、
零命中 / 非法动作 422（含动作索引）与整批原子性、动作数量 1～50 边界、
请求对象不被修改、命中与报告稳定排序、SHA-256 可复算；
以及 `/requests/materialize` 的 path simple/label/matrix 与 query form/
spaceDelimited/pipeDelimited/deepObject（数组、对象、explode 两种形态、
空集合、重复查询键）、`allowReserved` 与百分号编码（保留字符 / Unicode /
方括号结构符）、header simple 与大小写不敏感匹配、cookie form 序列化与 Cookie
拼接、默认 style/explode、布尔与数值渲染、嵌套结构 / null 值 422、
path-level 与 operation-level 参数合并（后者按 `(in, name)` 覆盖、
仅追加 operation 独有参数）、同级重复参数与仅大小写冲突的 header 参数 422
（含两处 Pointer）、参数与 3.1 path-level `$ref` 解析（悬空 / 外部 / 循环 422、
3.1 同级字段覆盖、3.0 忽略同级）、必填缺失 / 未知值 / 歧义 header 键 422、
路径模板一致性、400 文档 / 选择器 / 请求包体边界、多错误齐报与稳定排序、
输出与输入键序无关、请求对象不被修改；
以及 `/callbacks/resolve` 的固定字面量键、`$url` / `$method` / `$statusCode`
标量渲染、请求 path/query/header/body 与响应 header/body 表达式（含 JSON Pointer
深层取值、数组下标、整体 body 标量、`%HH` 名字解码、header 大小写不敏感）、
模板多表达式与字面文本拼接、结果按来源 Pointer 稳定排序、
缺值（缺 section / 缺字段 / 指针不可达 / `null` / 越界与非法数组下标）、
非标量插值与歧义 header 422、非法表达式（未知根 / 源、response.path、裸变量、
空 / 嵌套 / 未闭合花括号、非法指针转义、非法百分号、空键）、
Callback Object 与 Path Item 本地 `$ref` 解析（多跳、3.1 同级覆盖与 3.0 忽略同级、
表达式 Pointer 跟随到组件）、悬空 / 外部 / 非对象 / 循环引用 422（含回调与表达式
来源 Pointer、path-level `$ref`、根引用 `#` 拒绝）、400 文档 / 选择器 / context 信封边界、
无 callbacks 返回空列表、多错误齐报与输入对象不被修改等共 385 个用例。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 路由、请求/响应模型、输入校验（/audit、/slice、/compose、/examples/validate、/overlay/apply、/requests/materialize 与 /callbacks/resolve）
│   ├── audit.py         # 契约比较引擎（操作/参数/请求体/响应/schema）
│   ├── slicer.py        # 最小子契约切片（选择器校验、security 继承、$ref 闭包重建、SHA-256）
│   ├── composer.py      # 网关契约合成（前缀规范化、命名空间化、$ref 改写、冲突检测、来源映射）
│   ├── examples.py      # 媒体类型示例校验（方向化 readOnly/writeOnly/required、3.0/3.1 可空性、JSON Schema 关键字）
│   ├── overlay.py       # 有序覆盖动作（受限 JSONPath 解析、命中冻结、递归合并/倒序删除、SHA-256）
│   ├── materializer.py  # 请求物化（参数合并、循环安全 $ref、path/query/header/cookie 样式序列化、百分号编码）
│   ├── callbacks.py     # 回调 URL 解析（Runtime Expression、JSON Pointer body 片段、循环安全 Callback/Path Item $ref）
│   └── refresolver.py   # 本地 $ref 解析（RFC 6901 指针 + 循环检测）
├── tests/
│   ├── test_audit_api.py
│   ├── test_slice_api.py
│   ├── test_compose_api.py
│   ├── test_examples_api.py
│   ├── test_overlay_api.py
│   ├── test_materialize_api.py
│   └── test_callbacks_api.py
├── Dockerfile           # 容器封装全部依赖，非 root 用户运行，含 HEALTHCHECK
├── docker-compose.yml   # 一条命令启动，映射 8080 端口
├── requirements.txt
├── requirements-dev.txt
└── README.md
```
