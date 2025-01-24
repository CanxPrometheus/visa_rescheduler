import json
import os
from visa_rescheduler import VisaRescheduler

def main_loop():
    config_path = "config.json"
    # Config yükle
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    telegram_cfg = config.get("telegram", {})
    global_settings = config.get("global_settings", {})
    users = config.get("users", [])

    for idx, user_cfg in enumerate(users):
        username   = user_cfg["username"]
        password   = user_cfg["password"]
        schedule_id= user_cfg["schedule_id"]
        istaketed  = user_cfg.get("istaketed", False) 
        lastfour   = user_cfg["lastfour"]
        date_ranges= user_cfg["date_ranges"]
        scan_mode  = user_cfg["scan_mode"]

        if istaketed:
            print(f"\n--- {username} kullanıcısı zaten randevu almış (istaketed=True). Atlanıyor. ---")
            continue

        print(f"\n--- {username} kullanıcısı için işlem başlıyor ---")
        rescheduler = VisaRescheduler(
            username=username,
            password=password,
            schedule_id=schedule_id,
            lastfour=lastfour,
            date_ranges=date_ranges,
            scan_mode=scan_mode,
            telegram_cfg=telegram_cfg,
            global_settings=global_settings
        )

        randevu_bulundu = rescheduler.run()

        if randevu_bulundu:
            print(f"Randevu alındı! {username} için istaketed=true yapılacak.")

            users[idx]["istaketed"] = True

            config["users"] = users
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

        print(f"--- {username} kullanıcısı için işlem bitti ---\n")


if __name__ == "__main__":
    main_loop()