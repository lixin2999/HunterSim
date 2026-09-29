# HunterSim AI编码规则

> 本文档为HunterSim仿真系统AI辅助开发的强制性编码规则，IDE AI工具（Cursor、GitHub Copilot等）在生成代码时必须严格遵守。

---

## 角色设定：

你是一名资深的自动驾驶仿真系统架构师和Python高级开发工程师，精通CARLA仿真平台、ROS2通信框架、FastAPI后端开发、Docker容器化部署。你正在为HunterSim平台开发HunterSim。

## 背景上下文：

HunterSim平台是一个完整的自动驾驶解决方案，包含实车（HUNTER SE底盘车，尺寸820x640x310mm，质量60kg，轴距0.46m，轮距0.54m，最大速度4.8m/s，前轮阿克曼转向）和云端计算单元（AGX Orin）。HunterSim是该平台的VIL（车辆在环）虚拟仿真端，承担VIL实时仿真、场景仿真执行、传感器仿真、交通流仿真、数据回放与孪生、仿真评估、场景扩增七大核心职能。

系统采用C/S架构，分为三层： - 接口层：平台REST API、数据接口(Kafka)、可视化接口(WebSocket) - 仿真服务层：VIL映射服务、场景管理服务、传感器仿真服务、交通仿真服务、评估分析服务、数据回放服务、资源管理服务（基于Python FastAPI + ROS） - 仿真引擎层：CARLA Simulator (Unreal Engine)，包含物理引擎PhysX、渲染引擎、传感器模型、交通AI

部署架构为双机分离：CARLA服务端运行于Windows 11 + RTX 4070（原生支持），ROS2 Bridge与仿真管理服务运行于Ubuntu 22.04（与云端技术栈一致）。生产环境部署在Kubernetes集群中，CARLA引擎运行在GPU Pod中，仿真服务运行在CPU Pod中。

## 开发规范：

- Python代码遵循PEP 8规范，使用类型注解(type hints)

- 所有类和方法必须包含docstring（Google风格）

- 异常处理使用自定义异常类，禁止裸except

- 配置参数使用dataclass或pydantic模型管理

- 日志使用Python标准logging模块，分级输出(DEBUG/INFO/WARNING/ERROR)

- 异步操作使用asyncio，I/O密集型操作使用async/await

- 传感器数据格式必须与实车话题格式一致（ROS2标准消息类型）

- 坐标系统：CARLA使用左手坐标系(X东/Y北/Z上)，实车使用右手坐标系(X前/Y左/Z上)

- 代码必须包含单元测试框架兼容的模块化设计

- 所有外部依赖（CARLA API、Kafka、ROS2）必须通过抽象接口隔离，便于测试


## 项目上下文

- **项目名称**: HunterSim
- **技术栈**: Python 3.12 / FastAPI 0.100+ / CARLA 0.9.16 / ROS2 Humble / Docker / Kafka
- **架构**: C/S三层架构（接口层 -> 服务层 -> 引擎层）
- **部署**: 双机分离（Windows 11 + RTX 4070 运行CARLA / Ubuntu 22.04 运行服务）
- **GPU**: NVIDIA RTX 4070 (12GB) / CUDA 12.x

---

## 1. 语言与版本规则

### 1.1 Python版本

- 推荐版本：Python 3.12
- 禁止使用Python 2.x语法
- 禁止使用已废弃的语法特性（如print语句、旧式异常处理等）

### 1.2 依赖管理

- 使用pyproject.toml或requirements.txt管理依赖
- 明确指定依赖版本范围（如fastapi>=0.100,<0.110）
- 禁止使用latest标签
- 第三方库优先选择活跃维护的项目（最近1年有发布）

---

## 2. 代码风格规则

### 2.1 命名规范

| 元素类型 | 命名规则 | 示例 |
|---------|---------|------|
| 模块/包名 | 小写+下划线 | hunter_sim/engine/ |
| 类名 | 大驼峰 | HunterSEVehicle |
| 函数/方法名 | 小写+下划线 | get_vehicle_state() |
| 常量 | 大写+下划线 | MAX_SUBSTEPS |
| 私有属性 | 前缀下划线 | _internal_cache |
| 类型别名 | 大驼峰+Type后缀 | SensorConfigType |

### 2.2 文件结构规范

每个服务/模块遵循以下目录结构：

```
src/模块名/
  __init__.py      # 包初始化，导出公共接口
  main.py          # 入口文件
  config.py        # 配置管理
  models.py        # 数据模型
  services.py      # 业务逻辑
  repositories.py  # 数据访问
  utils.py         # 工具函数
  exceptions.py    # 自定义异常
tests/模块名/
  conftest.py      # 测试配置
  test_*.py        # 测试文件
tests/mocks/       # Mock类
```

### 2.3 导入规范

- 标准库导入优先
- 第三方库导入其次
- 本地应用导入最后
- 每组导入之间空一行
- 禁止使用通配符导入（from xxx import *）
- 绝对导入优先于相对导入

---

## 3. 类型注解规则

### 3.1 函数注解

- 所有函数必须包含完整的类型注解
- 返回值类型必须标注（包括None）
- 使用typing模块的Optional、Union、List、Dict等
- 复杂类型使用typing.NamedTuple或dataclass定义

### 3.2 配置类型

- 所有配置参数使用pydantic BaseModel或dataclass定义
- 配置类必须包含字段验证（validator）
- 敏感配置（密码、密钥）使用SecretStr类型
- 配置优先级：环境变量 > 配置文件 > 默认值

---

## 4. 异常处理规则

### 4.1 异常层次

定义统一的异常层次结构：

```
HunterSimError（基类）
  ConfigurationError（配置错误）
  ConnectionError（连接错误）
    CarlaConnectionError（CARLA连接错误）
    KafkaConnectionError（Kafka连接错误）
    ROS2ConnectionError（ROS2连接错误）
  ValidationError（数据验证错误）
  SimulationError（仿真运行错误）
    CarlaSimulationError（CARLA仿真错误）
    SensorSimulationError（传感器仿真错误）
  ResourceError（资源管理错误）
  TimeoutError（超时错误）
```

### 4.2 处理规范

- 禁止使用裸except（except:）
- 必须捕获具体异常类型
- 异常信息必须包含上下文（模块名、操作、参数）
- 资源清理使用try...finally或context manager
- 禁止在异常处理中吞掉异常（empty except block）
- 异步代码使用async context manager管理资源

---

## 5. 日志规范

### 5.1 日志级别

| 级别 | 使用场景 | 示例 |
|------|---------|------|
| DEBUG | 调试信息，详细执行流程 | 变量值、循环迭代 |
| INFO | 正常操作信息 | 服务启动、请求处理完成 |
| WARNING | 异常情况但不影响运行 | 配置使用默认值、降级处理 |
| ERROR | 错误但系统可继续运行 | 外部服务调用失败 |
| CRITICAL | 严重错误，系统可能不可用 | 数据库连接丢失 |

### 5.2 日志格式

- 使用Python标准logging模块
- 日志格式：[时间][级别][模块名] 消息
- 禁止使用print()输出日志
- 敏感信息（密码、token）禁止记录到日志
- 结构化日志使用JSON格式（生产环境）

---

## 6. 架构分层规则

### 6.1 分层架构

HunterSim采用严格的三层架构：

- **接口层（Interface Layer）**：REST API、WebSocket、Kafka消费者
  - 职责：请求接收、响应格式化、协议转换
  - 禁止包含业务逻辑
- **服务层（Service Layer）**：各业务服务
  - 职责：业务逻辑处理、协调各组件
  - 禁止直接操作CARLA API
- **引擎层（Engine Layer）**：CARLA Python API封装
  - 职责：CARLA API调用、传感器数据采集
  - 禁止包含业务逻辑

### 6.2 依赖方向

- 接口层依赖服务层
- 服务层依赖引擎层
- 引擎层不依赖任何其他层
- 禁止跨层调用（如接口层直接调用引擎层）
- 层间通信通过接口抽象（Protocol/ABC）

### 6.3 依赖注入

- 所有外部依赖通过构造函数注入
- 禁止在模块级别创建全局单例
- 使用依赖注入容器管理生命周期
- 测试时通过注入Mock对象实现隔离

---

## 7. CARLA API使用规则

### 7.1 连接管理

- 使用连接池管理CARLA客户端连接
- 连接超时配置：连接超时10s，读取超时30s
- 连接断开自动重连（最多3次，指数退避）
- 禁止在循环中频繁创建/销毁CARLA客户端

### 7.2 资源管理

- 所有Actor必须显式销毁（destroy()）
- 传感器回调必须正确注销
- 使用context manager管理临时资源
- 仿真结束后必须清理所有spawned actors

### 7.3 同步模式规范

- VIL模式必须使用同步模式（synchronous_mode=True）
- 固定步长20ms（50Hz），substepping启用
- 每步必须调用world.tick()
- 超时处理：tick等待超过500ms自动降级为异步模式

---

## 8. ROS2话题规范

### 8.1 话题命名

- 仿真话题前缀：/carla/
- 实车话题前缀：/lidar_points, /camera/, /imu/等
- 话题名称必须与实车完全一致
- 禁止使用特殊字符（除下划线）

### 8.2 消息类型

- 必须使用ROS2标准消息类型
- PointCloud2用于激光雷达数据
- Image用于相机数据
- Imu用于惯性传感器数据
- Odometry用于里程计数据

### 8.3 频率匹配

| 传感器 | 频率 |
|--------|------|
| LiDAR | 10Hz |
| RGB相机 | 30Hz |
| 深度相机 | 30Hz |
| IMU | 100Hz |
| Odometry | 50Hz |
| Chassis状态 | 10Hz |

---

## 9. 坐标系统规则

### 9.1 坐标系定义

| 坐标系 | 原点 | 轴向 | 手性 |
|--------|------|------|------|
| CARLA地图坐标 | 地图原点 | X东/Y北/Z上 | 左手系 |
| 实车odom坐标 | 车辆启动点 | X前/Y左/Z上 | 右手系 |

### 9.2 转换规则

- 所有内部计算统一使用弧度制
- API接口输入输出使用度制
- 坐标转换必须处理边界情况（180度翻转、原点附近）
- 高度信息：实车2D定位时Z坐标由地图路面高度决定

---

## 10. 测试规则

### 10.1 测试分层

- **单元测试**：测试纯函数和类方法（Mock外部依赖）
- **集成测试**：测试模块间交互（使用MockCARLA）
- **端到端测试**：完整场景验证（可选，CI/CD中定期运行）

### 10.2 测试规范

- 测试文件命名：test_模块名.py
- 测试类命名：Test类名
- 测试方法命名：test_方法名_场景
- 使用pytest fixtures管理测试数据
- 覆盖率目标：行覆盖率>=80%，分支覆盖率>=60%
- 禁止测试中sleep()，使用异步等待

---

## 11. 安全规则

### 11.1 输入验证

- 所有外部输入必须验证（API请求、配置文件、Kafka消息）
- 使用pydantic进行请求体验证
- 路径遍历攻击防护（禁止用户可控的文件路径）
- SQL注入防护（使用ORM或参数化查询）

### 11.2 敏感信息管理

- 密码、密钥、token禁止硬编码在代码中
- 使用环境变量或密钥管理服务
- 日志中脱敏处理敏感字段
- 配置文件中的敏感信息使用加密存储

### 11.3 网络安全

- API接口必须启用JWT认证
- 敏感接口启用HTTPS
- 限制API请求频率（Rate Limiting）
- CORS配置仅允许可信域名

---

## 12. 性能规则

### 12.1 计算性能

- 传感器数据处理使用numpy向量化操作
- 禁止在循环中创建大型对象
- 使用生成器处理大数据流
- 热点代码使用C扩展或Cython优化

### 12.2 I/O性能

- 数据库连接使用连接池
- 文件I/O使用异步操作
- 网络请求使用连接复用
- 大文件处理使用流式读取

### 12.3 内存管理

- 传感器数据使用环形缓冲区（RingBuffer）
- 禁止在循环中累积数据到列表
- 使用weakref管理回调引用
- 长期运行的服务定期执行垃圾回收

---

## 13. 文档规范

### 13.1 代码文档

- 所有公共类/函数必须包含Google风格docstring
- docstring包含：简介、参数说明、返回值说明、异常说明、示例
- 复杂算法必须包含注释说明设计思路
- 公共API变更必须更新文档

### 13.2 API文档

- 使用FastAPI自动生成Swagger文档
- 每个接口必须包含description和示例请求/响应
- 错误码必须文档化
- 版本变更记录在CHANGELOG.md中

---

## 14. AI辅助开发工作流规则

### 14.1 提示词使用顺序

1. 首先发送PROMPT-SYS-001（全局上下文初始化）
2. 按模块编号发送PROMPT-ENG-XXX开发模块
3. 发送PROMPT-API-XXX开发接口
4. 发送PROMPT-TEST-XXX编写测试
5. 发送PROMPT-CR-001进行代码审查
6. 发送PROMPT-DEPLOY-XXX配置部署

### 14.2 AI代码审查要点

- 架构一致性检查（是否跨层调用）
- CARLA API正确性（版本兼容性）
- 资源管理（是否有泄漏风险）
- 线程安全性（并发场景）
- 异常处理完整性
- 性能隐患排查
- 安全漏洞检查
- 传感器仿真准确性
- 坐标转换正确性
- ROS话题一致性

### 14.3 代码生成质量要求

- 生成的代码必须包含完整的类型注解
- 生成的代码必须包含docstring
- 生成的代码必须通过静态检查（flake8/mypy）
- 生成的代码必须包含对应的单元测试
- 生成的代码必须符合本章所有编码规则

---

**本章规则为HunterSim系统AI辅助开发的强制性规范，所有AI编码助手在生成代码时必须严格遵守。**
