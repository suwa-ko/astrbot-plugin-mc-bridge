# 开发与发布

## 目录

```text
astrbot_plugin_mc_bridge/   AstrBot 插件运行时
mod/                        NeoForge Mod
tests/                      Python 回归
docs/                       公开文档
.github/workflows/          CI
```

## Python 测试

测试依赖 AstrBot 运行时，不是只装插件 requirements 就能跑。

```bash
python -m pip install "astrbot==4.29.4" -r astrbot_plugin_mc_bridge/requirements.txt
MC_PLUGIN_FILE="$PWD/astrbot_plugin_mc_bridge/main.py" \
  python -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:MC_PLUGIN_FILE = "$PWD\astrbot_plugin_mc_bridge\main.py"
python -m unittest discover -s tests -v
```

当前基线 296 项。

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

- `python-tests.yml`：固定 AstrBot 4.29.4，安装插件依赖，跑 Python 回归。
- `mod-build.yml`：JDK 21 + Gradle wrapper，跑 Mod 构建和回归。
- `release.yml`：tag 时生成插件 zip、Mod jar 和 checksums，上传 GitHub Release。

## 发布

1. 更新 `astrbot_plugin_mc_bridge/metadata.yaml` 版本。
2. 更新 `mod/gradle.properties` 的 `mod_version`。
3. 更新 `CHANGELOG.md` 和 README 兼容表。
4. 打 tag：

   ```bash
   git tag v1.1.0
   git push origin v1.1.0
   ```

5. CI 生成 release 资产：
   - `astrbot_plugin_mc_bridge-<tag>.zip`
   - `astrbot_bridge-<version>.jar`
   - `checksums.txt`

## 代码约定

- 不提交运行时数据、日志、备份、截图和真实 token。
- 新动作必须有可验证的成功条件和失败路径。
- 部分完成必须返回 `partial`/`moved`/`produced` 等真实数据，禁止伪装成功。
- 技能参数在 direct tool、`mc_task_start`、dry-run 和 resume 之间共用校验。
- 修改协议或 capabilities 时同步更新 `protocol.md` 和兼容表。
