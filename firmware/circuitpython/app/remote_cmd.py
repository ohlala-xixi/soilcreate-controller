# remote_cmd.py - 远程下行指令的应用层
#
# 链路: 服务器 retained 发 cirpy-info/<cid> → 板子醒来 4G 订阅收到 → 本模块应用.
#
# 指令格式 (retained JSON):
#   { "rev": 7,                        # 单调递增版本号, 去重用
#     "config": { ... },               # 增量配置 (走 config.merge), 可省
#     "actions": ["read_now","reboot"] # 一次性动作, 可省
#   }
#
# 由 code.py 在 do_network_upload 成功、modem 仍连着、deinit 之前调用 handle_remote().
# 返回 signal dict 给主循环, 主循环按"智能生效策略"决定 live 改 / 软 reload / 重启.
#
# 配套 app/remote_cmd_nvm.py: rev 去重 + verify/rollback 状态机.

import json
from app import remote_cmd_nvm

BACKUP_PATH = "/config_prev.json"   # 应用前备份旧配置, 失败回滚用 (daily 模式可写)

# 本次 read_downlink 窗口内是否收到服务器 srv_ack (双向连通的真实证据;
# YunDTU 透传的 publish() 写串口必"成功", 不能当上报成功的依据)
_last_ack = False


def last_ack_seen() -> bool:
    return _last_ack


def read_downlink(modem, log=print, timeout_ms=3000):
    """读窗口内的所有下行 JSON, 分拣: 带 rev 的指令 + 服务器 srv_ack.

    返回 (cmd_dict_or_None, ack_seen). 同一窗口里指令和 ack 可能都到
    (modem.read_command 是持久缓冲, 一次返回一条), 所以循环读到超时.
    """
    cmd = None
    ack = False
    import time
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        remain_ms = int((deadline - time.monotonic()) * 1000)
        if remain_ms <= 0:
            break
        try:
            obj = modem.read_command(min(remain_ms, 1500))
        except Exception as e:
            log(f"[Remote] read err: {e}")
            break
        if not isinstance(obj, dict):
            if cmd is not None or ack:
                break   # 已有收获且这轮窗口安静了, 提前结束
            continue
        if "srv_ack" in obj:
            ack = True
            if cmd is not None:
                break
        elif obj.get("rev") is not None:
            if cmd is None:
                cmd = obj
            if ack:
                break
        # 其它 JSON (噪声/无 rev) 忽略继续读
    return (cmd, ack)


def handle_remote(config, modem, rs485_drivers, log=print, timeout_ms=3000):
    """读一条下行指令并应用. 返回 signal dict 或 None (无指令/旧指令/被禁用).
    同时把窗口内是否见到 srv_ack 存入 last_ack_seen() 供 verify/commit 用.

    signal: {rev, interval_changed, sleep_changed, live_sensor_channels[],
             need_reload, actions[], config_applied}
    """
    global _last_ack
    _last_ack = False
    if not config.get("remote.enabled", True):
        return None
    if modem is None or not hasattr(modem, "read_command"):
        return None

    cmd, _last_ack = read_downlink(modem, log, timeout_ms)
    if not isinstance(cmd, dict):
        return None

    rev = cmd.get("rev")
    try:
        rev = int(rev)
    except (TypeError, ValueError):
        log("[Remote] 指令 rev 非法, 忽略")
        return None
    if rev <= 0 or rev >= 0xFFFFFFFF:
        # 0xFFFFFFFF 会被 NVM 读回当成"空"导致去重失效; 负数/0 无意义
        log(f"[Remote] rev {rev} 超出范围 [1, 0xFFFFFFFE], 忽略")
        return None
    if not remote_cmd_nvm.is_new_rev(rev):
        log(f"[Remote] rev {rev} <= 已应用 {remote_cmd_nvm.read_applied_rev()}, 忽略 (retained 去重)")
        return None

    log(f"[Remote] 收到新指令 rev={rev}")
    cfg = cmd.get("config")
    actions = cmd.get("actions") or []

    sig = {
        "rev": rev,
        "interval_changed": False,
        "sleep_changed": False,
        "live_sensor_channels": [],
        "need_reload": False,
        "actions": list(actions),
        "config_applied": False,
    }

    if isinstance(cfg, dict) and cfg:
        # 0) 地址表归一化: 下行可用精简数组 [1,2,3], 内部统一存 [{"addr":1},...]
        _normalize_sensors(cfg)
        # 1) 分类 (必须在 merge 前, 要拿旧值比对决定是否重建 driver)
        _classify(config, cfg, rs485_drivers, sig)
        # 2) 备份旧配置 → 进验证态 → 合并落 NVM
        _backup_config(config, log)
        remote_cmd_nvm.begin_verify(rev)
        try:
            ok = config.merge(cfg)   # 递归合并 + save() 到 NVM
            if ok:
                sig["config_applied"] = True
                _sync_config_file(config, log)
                log(f"[Remote] 配置已合并进 NVM (rev={rev}); "
                    f"interval={sig['interval_changed']} sleep={sig['sleep_changed']} "
                    f"live_sensors={sig['live_sensor_channels']} reload={sig['need_reload']}")
            else:
                # save() 失败 (典型: 配置超 NVM 容量) — RAM 已被污染但 NVM 没动,
                # 重新 load 还原 RAM, 标 rolled_back 让 bridge 不清 retained (运维可见).
                # rev 保持已消耗, 防止同一条坏指令每周期重试.
                log(f"[Remote] merge 落 NVM 失败 (超容量?), 还原内存配置, rev={rev} 标记 rolled_back")
                try:
                    config.load()
                except Exception:
                    pass
                remote_cmd_nvm.mark_rolled_back()
                _remove_backup()
                sig["interval_changed"] = False
                sig["sleep_changed"] = False
                sig["live_sensor_channels"] = []
                sig["need_reload"] = False
        except Exception as e:
            log(f"[Remote] merge 失败: {e}")
    else:
        # 纯动作指令: 不进验证态, 但仍推进 rev 防止重复执行.
        # 状态标 committed 让 bridge 闭环清掉该条 retained (正在验证配置时不动状态).
        remote_cmd_nvm.write_applied_rev(rev)
        if not remote_cmd_nvm.is_verifying():
            remote_cmd_nvm.mark_committed_state()

    if actions:
        log(f"[Remote] actions: {actions}")
    return sig


def _sync_config_file(config, log):
    """远程配置应用成功后把 NVM 配置回写 /config.json (daily 模式可写).

    不回写的话, 之后任何一次 #sync_config (PC 工具写盘必发) 会用盘上旧文件
    把远程改动静默覆盖回去. flash 模式盘只读, 写失败无害 (开发场景)."""
    try:
        with open("/config.json", "w") as f:
            json.dump(config.get_all(), f)
        log("[Remote] /config.json 已同步")
    except Exception:
        pass


def _normalize_sensors(cfg):
    """下行地址表精简写法 → 内部统一格式.

    允许 "sensors":[1,2,3] (纯地址数组), 落 NVM 前转成 [{"addr":1},...],
    这样采集循环/上报/BLE 等沿用同一套 {"addr":..} 结构, 无需改动.
    已是 {"addr":..} 对象形式的元素原样保留 (两种写法都收).
    """
    for ch in (1, 2):
        chc = cfg.get(f"rs485_{ch}")
        if not isinstance(chc, dict) or "sensors" not in chc:
            continue
        s = chc.get("sensors")
        if not isinstance(s, list):
            continue
        norm = []
        for item in s:
            if isinstance(item, bool):
                continue  # bool 是 int 子类, 先挡掉异常布尔
            if isinstance(item, dict):
                norm.append(item)
            elif isinstance(item, int):
                norm.append({"addr": item})
            elif isinstance(item, float):
                norm.append({"addr": int(item)})
            elif isinstance(item, str):
                try:
                    norm.append({"addr": int(item.strip())})
                except ValueError:
                    continue
        chc["sensors"] = norm


def _classify(config, cfg, rs485_drivers, sig):
    """对比旧配置, 标记: 间隔/休眠 live 改, 地址 live 重读, 结构改动需 reload"""
    sysc = cfg.get("system") or {}
    if "interval_preset" in sysc or "interval_custom_min" in sysc:
        sig["interval_changed"] = True
    if "sleep_mode" in sysc:
        sig["sleep_changed"] = True

    for ch in (1, 2):
        chc = cfg.get(f"rs485_{ch}")
        if not isinstance(chc, dict):
            continue
        # 结构性改动 (协议/波特率/启用) → driver/protocol 必须重建
        struct = False
        if "protocol" in chc and chc["protocol"] != config.get(f"rs485_{ch}.protocol"):
            struct = True
        if "baud" in chc and chc["baud"] != config.get(f"rs485_{ch}.baud"):
            struct = True
        if "enabled" in chc and bool(chc["enabled"]) != bool(config.get(f"rs485_{ch}.enabled", False)):
            struct = True
        if struct:
            sig["need_reload"] = True
        if "sensors" in chc:
            if (ch in rs485_drivers) and not struct:
                # 仅地址增删、口已有 driver → 主循环 live 重读, 零停机
                sig["live_sensor_channels"].append(ch)
            else:
                # 口开机时没建 driver, 或同时改了结构 → 软 reload 重建
                sig["need_reload"] = True
    # 网络字段 (broker/4g/wifi): 下周期 _make_modem 会按新配置重连重配, 无需 reload


def _backup_config(config, log):
    """应用前把当前完整配置备份到盘, 供失败回滚. flash 只读盘写不了则放弃备份."""
    try:
        with open(BACKUP_PATH, "w") as f:
            json.dump(config.get_all(), f)
        log(f"[Remote] 旧配置已备份 → {BACKUP_PATH}")
        return True
    except Exception as e:
        # flash 模式 (开发) 盘对设备只读; 现役 daily 模式可写. 写不了仍应用, 只失去回滚兜底.
        log(f"[Remote] 备份失败 (flash 只读?): {e} — 仍应用但无回滚")
        return False


def commit_applied(log=print):
    """本周期上报成功 → 坐实新配置, 删备份"""
    remote_cmd_nvm.commit()
    _remove_backup()
    log("[Remote] 配置验证通过, 已 commit")


def do_rollback(config, log=print):
    """连续上报失败达阈值 → 用备份恢复旧配置 (调用方随后 reboot)"""
    log("[Remote] 配置验证失败, 回滚旧配置...")
    restored = False
    try:
        with open(BACKUP_PATH, "r") as f:
            prev = json.load(f)
        if isinstance(prev, dict):
            config.set_all(prev)   # 覆盖 self.config + save 到 NVM
            restored = True
            log("[Remote] 旧配置已恢复进 NVM")
    except Exception as e:
        log(f"[Remote] 读备份失败: {e} (无备份可恢复, 仅清验证态)")
    remote_cmd_nvm.mark_rolled_back()
    _remove_backup()
    return restored


def _remove_backup():
    import os
    try:
        os.remove(BACKUP_PATH)
    except Exception:
        pass
