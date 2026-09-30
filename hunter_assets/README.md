# hunter_assets

HUNTER 自定义仿真资源目录，构建 CARLA 引擎镜像时由 `docker/Dockerfile.carla`
COPY 到镜像内 `/home/carla/carla/PythonAPI/hunter_assets/`（设计文档 §15.3）。

建议目录结构（对应 §10.4.1 资源仓库）：

```
hunter_assets/
├── vehicles/        # HUNTER SE 等车辆模型（.udav 源或打包蓝图）
├── pedestrians/     # 行人模型
├── props/           # 静态物体模型
└── resources/       # 挂载共享卷：maps/custom 自定义地图等
```

> 模型资源需经平台审核后方可进入资源白名单（设计文档 §14.3 资源白名单）。
