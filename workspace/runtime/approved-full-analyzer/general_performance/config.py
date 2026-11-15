_PERCEPTION_STATE = {
    '0': 'slow_driver', '1': 'parking', '2': 'HNOA', '3': 'PGear',
    '4': 'fast_driver', '5': 'CNOA', '6': 'switch_parking_driving', '9': 'user_RGear'
}
_PERCEPTION_STATE_TEXT = "slow_driver/parking/HNOA/PGear/fast_driver/CNOA/switch_parking_driving/user_RGear"

_FSDA_M100_CONTAINERS = [
    "datarec_container", "parkvlm_container", "vla_planning_container",
    "vla_arch_container", "e2e_parking_runner_container", "bev_parking_container",
    "e2e_container", "rc_container", "e2e_planning_container",
    "orina_can_server_cpu", "env_container", "realtime_container",
    "lidar_container", "highway_planning_container", "app_container",
    "vla_parking_container",
]

CONFIG_PATH = {
    # 日志soc分布，日志文件名
    # m100 ultra的对应关系
    "FSDA_M100_ULTRA": ["FSDA_M100"] + _FSDA_M100_CONTAINERS,
    "FSDB_M100_ULTRA": ["FSDB_M100", "lidar_container"],

    # m100 max的对应关系
    "FSDA_M100_MAX": ["FSDA_M100"] + _FSDA_M100_CONTAINERS,

    # thor 对应关系
    "FSDA_Thor": ["Thor_soc", "datarec_container", "parkvlm_container", "vla_planning_container", "vla_arch_container", "e2e_parking_runner_container", "bev_parking_container", "e2e_container", "rc_container", "e2e_planning_container", "orina_can_server_cpu", "env_container", "realtime_container", "lidar_container", "highway_planning_container", "app_container"],

    # orin 对应关系
    "FSDA_Orin": ["FSDA_OrinX", "datarec_container", "orina_can_server_cpu", "env_container", "realtime_container", "lidar_container", "highway_planning_container", "parkvlm_container"],
    "FSDB_Orin": ["FSDB_OrinX", "vla_planning_container", "vla_arch_container", "bev_parking_container", "e2e_container", "rc_container", "e2e_planning_container", "e2e_parking_runner_container"],
}


CONFIG_M100_ULTRA = {
    "summary": {
        # m100 ultra 模版
        "template": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        # m100 ultra 配置
        "value_config_url": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        "not_plot": False
    },
    "perception_state": _PERCEPTION_STATE,
    "perception_state_text": _PERCEPTION_STATE_TEXT,
}


CONFIG_M100_MAX = {
    "summary": {
        # m100 max 模版
        "template": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        # m100 max 配置
        "value_config_url": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        "not_plot": False
    },
    "perception_state": _PERCEPTION_STATE,
    "perception_state_text": _PERCEPTION_STATE_TEXT,
}


CONFIG_THOR = {
    # 多个主题的perf汇总
    "summary": {
        # thor 模版
        "template": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        # thor 配置
        "value_config_url": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        "not_plot": False
    },
    "perception_state": _PERCEPTION_STATE,
    "perception_state_text": _PERCEPTION_STATE_TEXT,
}

CONFIG_ORIN = {
    # 多个主题的perf汇总
    "summary": {
        # orin 模版
        "template": ['REPLACE_APPROVED_RESOURCE', "0Ywvtv"],
        # orin 配置
        "value_config_url": ['REPLACE_APPROVED_RESOURCE', "8b87a0"],
        "not_plot": False
    },
    "perception_state": _PERCEPTION_STATE,
    "perception_state_text": _PERCEPTION_STATE_TEXT,
}

CONFIG_MAPS = {
    "m100-ultra": CONFIG_M100_ULTRA,
    "m100-max":   CONFIG_M100_MAX,
    "thor":       CONFIG_THOR,
    "orin":       CONFIG_ORIN,
}
