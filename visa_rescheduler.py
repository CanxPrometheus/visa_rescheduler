import time
import json
import random
import requests
import traceback
from datetime import datetime, timedelta
from http.client import RemoteDisconnected

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager

class VisaRescheduler:
    BASE_URL = "https://ais.usvisa-info.com/tr-tr/niv"
    SIGN_IN_LINK = BASE_URL + "/users/sign_in"
    SIGN_OUT_LINK = BASE_URL + "/users/sign_out"

    # "Devam Et" butonunu XPATH ile bulmak için metin
    REGEX_CONTINUE = "Devam Et"

    # JavaScript ile zaman çekmek için
    JS_SCRIPT = (
        "var req = new XMLHttpRequest();"
        "req.open('GET', '%s', false);"  # synchronous request
        "req.setRequestHeader('Accept', 'application/json, text/javascript, */*; q=0.01');"
        "req.setRequestHeader('X-Requested-With', 'XMLHttpRequest');"
        "req.setRequestHeader('Cookie', '_yatri_session=%s');"
        "req.send(null);"
        "return req.responseText;"
    )

    def __init__(
        self,
        username: str,
        password: str,
        schedule_id: str,
        lastfour: bool,
        date_ranges: dict,
        scan_mode: str,
        telegram_cfg: dict,
        global_settings: dict
    ):
        """
        Parametrelerinizi constructor'da saklıyoruz.
        """
        self.username = username
        self.password = password
        self.schedule_id = schedule_id
        self.lastfour = lastfour
        self.date_ranges = date_ranges
        self.scan_mode = scan_mode

        # Telegram bilgileri
        self.bot_token = telegram_cfg.get("bot_token")
        self.chat_id = telegram_cfg.get("chat_id")

        # Global ayarlar
        self.WORK_LIMIT_TIME = global_settings.get("schedule_check_limit_hours", 24)
        self.REFRESH_INTERVAL_SECONDS = global_settings.get("refresh_interval_seconds", 1500)
        self.REFRESH_SLEEP = global_settings.get("refresh_sleep", 30)
        self.WAIT_IF_SERVER_ERROR_HOURS = global_settings.get("wait_if_server_error_hours", 5)
        self.WAIT_IF_SINGLE_FACILITY_ERROR_MINUTES = global_settings.get("wait_if_single_facility_error_minutes", 15)

        retry_bounds = global_settings.get("retry_time_bounds", [7, 20])
        self.RETRY_TIME_L_BOUND, self.RETRY_TIME_U_BOUND = retry_bounds[0], retry_bounds[1]

        out_of_range_bounds = global_settings.get("out_of_range_wait_bounds", [7, 40])
        self.OUT_OF_RANGE_WAIT_LOW, self.OUT_OF_RANGE_WAIT_HIGH = out_of_range_bounds[0], out_of_range_bounds[1]

        # Selenium driver başta None
        self.driver = None

        # APPOINTMENT_URL: LASTFOUR'a göre ek parametre
        if self.lastfour:
            self.APPOINTMENT_URL = (
                f"{self.BASE_URL}/schedule/{self.schedule_id}/appointment"
                "?confirmed_limit_message=1&commit=Continue"
            )
        else:
            self.APPOINTMENT_URL = (
                f"{self.BASE_URL}/schedule/{self.schedule_id}/appointment"
            )

    def send_telegram_message(self, text: str) -> None:
        """Telegram'a text şeklinde mesaj gönderir."""
        if not self.bot_token or not self.chat_id:
            print(f"Telegram bilgileri tanımsız. Mesaj gönderilemedi: {text}")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            requests.post(url, data=payload, timeout=10)
        except Exception as exc:
            print(f"Telegram gönderim hatası: {exc}")

    def log(self, msg: str, telegram: bool = False) -> None:
        """Konsola log atar, telegram=True ise aynı mesajı telegram'a da iletir."""
        print(msg)
        if telegram:
            self.send_telegram_message(msg)

    def init_driver(self):
        """Selenium driver başlat (görünür)."""
        chrome_options = webdriver.ChromeOptions()
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )

    def perform_login(self) -> None:
        """Sisteme giriş yap."""
        driver = self.driver
        driver.get(self.SIGN_IN_LINK)
        time.sleep(1.0)

        if not driver.current_url.startswith(self.SIGN_IN_LINK):
            time.sleep(3)

        try:
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.NAME, "commit")))
        except TimeoutException:
            self.log("Giriş sayfası yüklenemedi veya 'commit' butonu bulunamadı.", telegram=True)
            return

        # Alttan çıkan "bounce" linkini tıkla (varsa)
        try:
            self.auto_action(
                "Click bounce",
                By.XPATH,
                '//a[@class="down-arrow bounce"]',
                "click"
            )
        except NoSuchElementException:
            pass

        self.auto_action("Email", By.ID, "user_email", "send", self.username)
        self.auto_action("Password", By.ID, "user_password", "send", self.password)
        self.auto_action("Privacy", By.CLASS_NAME, "icheckbox", "click", "")
        self.auto_action("Enter Panel", By.NAME, "commit", "click", "")

        # "Devam Et" yazısını bekliyoruz
        try:
            WebDriverWait(driver, 60).until(
                EC.presence_of_element_located((By.XPATH, f"//a[contains(text(), '{self.REGEX_CONTINUE}')]"))
            )
            self.log("Giriş başarılı!")
        except TimeoutException:
            self.log("Giriş başarılı olmadı veya 'Devam Et' butonu bulunamadı.", telegram=True)

    def perform_logout(self) -> None:
        """Sistemden çıkış."""
        try:
            self.driver.get(self.SIGN_OUT_LINK)
            time.sleep(2)
        except Exception as e:
            print(f"Çıkış esnasında hata oluştu: {e}")
        finally:
            if self.driver:
                self.driver.quit()
                self.driver = None
                print("Tarayıcı kapatıldı.")

    def auto_action(self, label: str, by: By, locator: str, action: str, value: str = "", sleep_time: float = 1.0) -> None:
        """Element bulma/etkileşim fonksiyonu (Selenium)."""
        print(f"{label}:", end=" ")
        driver = self.driver
        try:
            item = driver.find_element(by, locator)
        except NoSuchElementException:
            print(f"Element bulunamadı: {locator}")
            return

        if action.lower() == 'send':
            item.send_keys(value)
        elif action.lower() == 'click':
            item.click()
        else:
            raise ValueError("Geçersiz 'action' parametresi!")

        print("OK!")
        if sleep_time:
            time.sleep(sleep_time)

    def get_session_cookie(self) -> str:
        """_yatri_session çerezini döndür."""
        if not self.driver:
            return ""
        cookie = self.driver.get_cookie("_yatri_session")
        return cookie["value"] if cookie else ""

    def fetch_data(self, url: str, headers: dict) -> requests.Response:
        """GET isteği atar, hata alırsa Exception fırlatır."""
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectionError, RemoteDisconnected) as ce:
            raise ConnectionError(f"Connection aborted: {ce}")
        except requests.exceptions.HTTPError as he:
            raise ConnectionError(f"HTTP Error: {he}")
        except Exception as e:
            raise ConnectionError(f"Beklenmeyen istek hatası: {e}")

    def get_available_dates(self, facility_id: int) -> list:
        """Verilen facility_id için tarih listesini döndür."""
        session_value = self.get_session_cookie()
        if not session_value:
            self.log("Session cookie boş. Giriş yapmayı kontrol edin.", telegram=True)
            return []

        url = f"{self.BASE_URL}/schedule/{self.schedule_id}/appointment/days/{facility_id}.json?appointments[expedite]=false"
        print(f"Requesting dates from: {url}")

        headers = {
            "User-Agent": self.driver.execute_script("return navigator.userAgent;"),
            "Referer": self.APPOINTMENT_URL,
            "Cookie": f"_yatri_session={session_value}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest"
        }

        try:
            response = self.fetch_data(url, headers)
            data = response.json()
            print(f"Available dates for facility {facility_id}: {data}")
            return data or []
        except ConnectionError as conn_err:
            self.log(f"{facility_id} -> Bağlantı hatası: {conn_err}")
            return None
        except Exception as e:
            self.log(f"JSON parse error: {e}", telegram=True)
            return []

    def get_time_url(self, facility_id: int, date_str: str) -> str:
        return (
            f"{self.BASE_URL}/schedule/{self.schedule_id}/appointment/times/{facility_id}.json"
            f"?date={date_str}&appointments[expedite]=false"
        )

    def get_time_for_date(self, date_str: str, facility_id: int) -> str or None:
        """Seçili tarih için son slot (en geç) döndür."""
        session = self.get_session_cookie()
        if not session:
            self.log("Session cookie boş. Giriş yapmayı kontrol edin.", telegram=True)
            return None

        url = self.get_time_url(facility_id, date_str)
        try:
            content = self.driver.execute_script(self.JS_SCRIPT % (url, session))
            data = json.loads(content)

            times = data.get("available_times")
            print(f"Available times for facility {facility_id}: {data}")
            if not times:
                return None
            return times[-1]
        except Exception as e:
            self.log(f"Time fetching error for date {date_str} and facility {facility_id}: {e}", telegram=True)
            return None

    def is_in_period(self, date_str: str, start_dt: datetime, end_dt: datetime) -> bool:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return start_dt <= d <= end_dt

    def find_valid_date(self, dates: list, facility_id: int) -> str or None:
        """Tarih aralığına uygun ilk tarihi döndür."""
        if str(facility_id) not in self.date_ranges:
            # Bu kullanıcıda o facility yoksa None
            return None

        start_str, end_str = self.date_ranges[str(facility_id)]
        psd = datetime.strptime(start_str, "%Y-%m-%d")
        ped = datetime.strptime(end_str, "%Y-%m-%d")

        for item in dates:
            date_str = item.get("date")
            if not date_str:
                continue
            if self.is_in_period(date_str, psd, ped):
                return date_str
        return None

    def try_multiple_locators(self, locator_list, timeout=10):
        driver = self.driver
        for method, locator in locator_list:
            try:
                element = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((method, locator))
                )
                return element
            except (TimeoutException, NoSuchElementException):
                pass
        return None

    def go_to_appointment_page(self) -> None:
        """APPOINTMENT_URL sayfasına gidip 'Devam Et' butonu varsa tıkla."""
        self.driver.get(self.APPOINTMENT_URL)
        time.sleep(2)

        possible_locators = [
            (By.XPATH, '//*[@id="main"]/div[3]/form/div[2]/div/input'),
            (By.CSS_SELECTOR, '#main input[value="Devam Et"]'),
            (By.XPATH, '//input[@name="commit"]')
        ]
        btn_continue = self.try_multiple_locators(possible_locators, timeout=6)
        if btn_continue:
            btn_continue.click()
            print("'Devam Et' butonuna basıldı.")
            time.sleep(2)
        else:
            print("Devam Et butonu bulunamadı. Geçiliyor...")

    def reschedule(self, date_str: str, facility_id: int) -> tuple:
        """Verilen date_str ve facility_id için yeni randevu saati ayarla."""
        appointment_time = self.get_time_for_date(date_str, facility_id)
        if not appointment_time:
            return ("FAIL", f"Saat bulunamadı: {date_str}")

        # Oturum çerezi
        session = self.get_session_cookie()
        if not session:
            self.log("Session cookie bulunamadı. Tarayıcı yeniden başlatılıyor...", telegram=True)
            try:
                self.driver.quit()
            except:
                pass

            self.init_driver()
            self.perform_login()
            session = self.get_session_cookie()
            if not session:
                return ("FAIL", "Session cookie yeniden de alınamadı.")

        driver = self.driver
        try:
            data = {
                "authenticity_token": driver.find_element(By.NAME, 'authenticity_token').get_attribute('value'),
                "confirmed_limit_message": driver.find_element(By.NAME, 'confirmed_limit_message').get_attribute('value'),
                "use_consulate_appointment_capacity": driver.find_element(By.NAME, 'use_consulate_appointment_capacity').get_attribute('value'),
                "appointments[consulate_appointment][facility_id]": facility_id,
                "appointments[consulate_appointment][date]": date_str,
                "appointments[consulate_appointment][time]": appointment_time,
            }
        except NoSuchElementException as e:
            self.log(f"Form element bulunamadı: {e}", telegram=True)
            return ("FAIL", f"Form element bulunamadı: {e}")

        headers = {
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Referer": self.APPOINTMENT_URL,
            "Cookie": "_yatri_session=" + session
        }

        print(f"-> Randevu ayarlanıyor: {date_str} / {appointment_time}")
        try:
            response = requests.post(self.APPOINTMENT_URL, headers=headers, data=data)
        except Exception as e:
            self.log(f"POST isteği sırasında hata: {e}", telegram=True)
            return ("FAIL", f"POST isteği sırasında hata: {e}")

        if "Randevunuz İçin Başarılı Bir Şekilde Zaman Aldınız" in response.text:
            success_msg = f"Başarıyla planlandı! Tarih: {date_str}, Saat: {appointment_time}"
            return ("SUCCESS", success_msg)
        else:
            with open("reschedule_response.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            self.log("Yenileme başarısız. Response HTML kaydedildi.", telegram=True)
            return ("FAIL", f"Yenileme başarısız -> {date_str}, {appointment_time}")

    def city_name(self, facility_id: int) -> str:
        if facility_id == 124:
            return "ANKARA"
        elif facility_id == 125:
            return "İSTANBUL"
        return f"Facility {facility_id}"

    def get_facilities_by_scan_mode(self) -> list:
        """SCAN_MODE'e göre şehir listesi döndür."""
        sm = self.scan_mode.lower()
        if sm == "ankara":
            return [124]
        elif sm == "istanbul":
            return [125]
        else:
            # both
            return [124, 125]

    def run(self) -> bool:
        """
        Tek bir kullanıcı (self.username, vs.) için tüm döngüyü çalıştırır.
        Randevu başarıyla bulunursa True, aksi halde False döndürür.
        """
        self.init_driver()
        self.perform_login()
        self.go_to_appointment_page()

        facilities = self.get_facilities_by_scan_mode()
        next_check_time = {fac: datetime.now() for fac in facilities}
        facility_error = {fac: False for fac in facilities}

        start_time = time.time()
        last_refresh = time.time()
        request_count = 0
        randevu_alindi = False  # Randevu alınıp alınmadığının bayrağı

        while True:
            now = datetime.now()
            request_count += 1
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")

            print("-" * 60)
            self.log(f"[Request {request_count}] Zaman: {now_str}")

            found_any_date = False
            no_dates_count = 0
            connection_error_count = 0

            try:
                # Tüm şehirler
                for facility_id in facilities:
                    city = self.city_name(facility_id)

                    # Zamana bak
                    if now < next_check_time[facility_id]:
                        continue

                    self.log(f"*** {city} kontrol ediliyor ***")

                    dates = self.get_available_dates(facility_id)
                    if dates is None:
                        # Connection hatası
                        self.log(f"{city}: Connection aborted veya HTTP Error alındı.", telegram=True)
                        connection_error_count += 1
                        facility_error[facility_id] = True
                        next_check_time[facility_id] = now + timedelta(minutes=self.WAIT_IF_SINGLE_FACILITY_ERROR_MINUTES)
                        continue

                    facility_error[facility_id] = False

                    if not dates:
                        no_dates_count += 1
                        wait_sec = random.randint(self.OUT_OF_RANGE_WAIT_LOW, self.OUT_OF_RANGE_WAIT_HIGH)
                        self.log(f"{city} tarih listesi boş. ~{wait_sec}sn bekle.")
                        next_check_time[facility_id] = now + timedelta(seconds=wait_sec)
                        continue

                    valid_date = self.find_valid_date(dates, facility_id)
                    if valid_date:
                        self.log(f"{city}: Uygun tarih bulundu -> {valid_date}", telegram=True)
                        result_title, result_msg = self.reschedule(valid_date, facility_id)
                        if result_title == "SUCCESS":
                            self.log(f"{city}: {result_msg}", telegram=True)
                            found_any_date = True
                            randevu_alindi = True  # Randevu alınmış oldu
                            break
                        else:
                            self.log(f"{city}: {result_msg}", telegram=True)
                            wait_sec = random.randint(self.OUT_OF_RANGE_WAIT_LOW, self.OUT_OF_RANGE_WAIT_HIGH)
                            next_check_time[facility_id] = now + timedelta(seconds=wait_sec)
                    else:
                        # Aralığa uyan tarih yok
                        wait_sec = random.randint(self.OUT_OF_RANGE_WAIT_LOW, self.OUT_OF_RANGE_WAIT_HIGH)
                        self.log(f"{city}: Aralığa uyan tarih yok. ~{wait_sec}sn bekle.")
                        next_check_time[facility_id] = now + timedelta(seconds=wait_sec)

                # Eğer döngü esnasında found_any_date True olduysa => randevu alındı
                if found_any_date:
                    break

                if no_dates_count == len(facilities):
                    wait_sec = random.randint(self.OUT_OF_RANGE_WAIT_LOW, self.OUT_OF_RANGE_WAIT_HIGH)
                    self.log(f"Tüm şehirler boş döndü! ~{wait_sec}sn bekle...", telegram=False)
                    time.sleep(wait_sec)
                    continue

                if connection_error_count == len(facilities):
                    self.log("Tüm şehirlerde bağlantı hatası alındı. 5 saat bekleyecek...", telegram=True)
                    self.perform_logout()
                    time.sleep(self.WAIT_IF_SERVER_ERROR_HOURS * 3600)
                    self.init_driver()
                    self.perform_login()
                    self.go_to_appointment_page()
                    last_refresh = time.time()
                    continue

                # Süre sınırı aşıldı mı?
                elapsed_hours = (time.time() - start_time) / 3600
                if elapsed_hours > self.WORK_LIMIT_TIME:
                    self.log(f"Çalışma süresi doldu ({self.WORK_LIMIT_TIME} saat). Program sonlandırılıyor.", telegram=True)
                    break

                # Rastgele bekleme
                retry_wait = random.randint(self.RETRY_TIME_L_BOUND, self.RETRY_TIME_U_BOUND)
                self.log(f"Tekrar deneme öncesi bekleme: {retry_wait} sn")
                time.sleep(retry_wait)

                # Periyodik yenileme
                if (time.time() - last_refresh) > self.REFRESH_INTERVAL_SECONDS:
                    self.log("Periyodik yenileme vakti geldi, tarayıcı kapatılıp yeniden açılacak...", telegram=True)
                    self.perform_logout()
                    time.sleep(self.REFRESH_SLEEP)
                    self.init_driver()
                    self.perform_login()
                    self.go_to_appointment_page()
                    last_refresh = time.time()

            except (TimeoutException, ConnectionError, requests.RequestException, WebDriverException) as critical_exc:
                err_msg = f"Sunucu/Selenium bağlantı hatası: {critical_exc}"
                self.log(err_msg, telegram=True)

                self.perform_logout()
                time.sleep(self.WAIT_IF_SERVER_ERROR_HOURS * 3600)
                self.init_driver()
                self.perform_login()
                self.go_to_appointment_page()
                last_refresh = time.time()
                continue

            except Exception as exc:
                err_msg = f"Beklenmeyen hata (ana döngü): {exc}"
                self.log(err_msg, telegram=True)
                traceback.print_exc()

                self.perform_logout()
                time.sleep(30)
                self.init_driver()
                self.perform_login()
                self.go_to_appointment_page()
                last_refresh = time.time()
                continue

        self.perform_logout()
        self.log("Bu kullanıcı için döngü sonlandı.", telegram=True)
        return randevu_alindi
