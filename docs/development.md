# 开发与发布

## 目录

```text
main.py / *.py              AstrBot 插件运行时（仓库根目录）
metadata.yaml               插件市场元数据
_conf_schema.json           插件配置 schema
requirements.txt            插件 Python 依赖
mod/                        NeoForge Mod
tests/                      Python 回归
docs/                       公开文档
.github/workflows/          CI
```

## Python 测试

测试依赖 AstrBot 运行时，不是只装插件 requirements 就能跑。

```bash
python -m pip install "astrbot==4.29.4" -r requirements.txt
MC_PLUGIN_FILE="$PWD/main.py" \
  python -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:MC_PLUGIN_FILE = "$PWD\main.py"
python -m unittest discover -s tests -v
```

当前基线 314 项。

## Mod 构建

```bash
cd mod
./gradlew assemble bridgeRegression navigationRegression survivalRegression
```

包含：

- `survivalRegression`
- `navigationRegression`
- `bridgeRegression`

构建产物：`mod/build/libs/astrbot_bridge-<version>.jar`。

## CI

- `python-tests.yml`：固定 AstrBot 4.29.4，安装根目录 `requirements.txt`，跑 Python 回归。
- `mod-build.yml`：JDK 21 + Gradle wrapper，跑 Mod 构建和回归。
- `release.yml`：tag 时生成插件 zip、Mod jar 和 checksums，上传 GitHub Release。

## 发布

1. 更新根目录 `metadata.yaml` 版本。
2. 更新 `mod/gradle.properties` 的 `mod_version`。
3. 更新 `CHANGELOG.md` 和 README 兼容表。
4. 打 tag：

   ```bash
   git tag v1.2.1
   git push origin v1.2.1
   ```

5. CI 生成 release 资产：
   - `astrbot_plugin_mc_bridge-<tag>.zip`（只包含根目录插件文件）
   - `astrbot_bridge-<version>.jar`
   - `checksums.txt`

## 插件市场

AstrBot Cloud 的 GitHub 发布流程只解析仓库默认分支根目录的 `metadata.yaml` / `metadata.yml`。因此插件文件必须位于仓库根目录，不能只放在子目录里。`metadata.yaml` 需要包含 `name`、`desc`、`version`、`author`、`repo`；可选 `tags`、`social_link`、`astrbot_version`。

发布时如果 AstrBot Cloud 返回 Cloudflare 502，先确认 `/api/v1/market/options` 中的 `publish_enabled` 和 `zip_upload_enabled` 状态，再查看 AstrBot Cloud 的 issue 列表；这通常是服务端 GitHub 解析通道故障，不是插件包本身的问题。

## 代码约定

- 不提交运行时数据、日志、备份、截图和真实 token。
- 新动作必须有可验证的成功条件和失败路径。
- 部分完成必须返回 `partial`/`moved`/`produced` 等真实数据，禁止伪装成功。
- 技能参数在 direct tool、`mc_task_start`、dry-run 和 resume 之间共用校验。
- 修改协议或 capabilities 时同步更新 `protocol.md` 和兼容表。
