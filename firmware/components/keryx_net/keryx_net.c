#include "keryx_net.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include <fcntl.h>
#include "keryx_console.h"
#include "nvs.h"
#include "nvs_flash.h"

#define NVS_NAMESPACE "keryx"
#define URL_MAX 200
#define TOKEN_MAX 200
#define SCAN_MAX 20
#define RETRY_MIN_S 1
#define RETRY_MAX_S 30
#define CHECK_TIMEOUT_S 3

static const char *const KEYS[] = {"ssid", "password", "bridge", "token"};
static const char *const SECRET_KEYS[] = {"password", "token"};

static esp_netif_t *netif;
static esp_timer_handle_t retry_timer;
static int retry_s = RETRY_MIN_S;
static volatile bool connected;
static bool started;
static char start_error[64];  // why keryx_net_start() failed, for `status`

const char *keryx_net_id(void)
{
    static char id[16];
    if (id[0] == '\0') {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        snprintf(id, sizeof(id), "keryx-%02x%02x%02x", mac[3], mac[4], mac[5]);
    }
    return id;
}

bool keryx_net_connected(void)
{
    return connected;
}

// Reads a setting into buf; false if unset (buf is then empty).
static bool setting_get(const char *key, char *buf, size_t size)
{
    buf[0] = '\0';
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) {
        return false;
    }
    size_t len = size;
    bool ok = nvs_get_str(nvs, key, buf, &len) == ESP_OK;
    nvs_close(nvs);
    if (!ok) {
        buf[0] = '\0';
    }
    return ok;
}

static esp_err_t setting_set(const char *key, const char *value)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err == ESP_OK) {
        err = nvs_set_str(nvs, key, value);
        if (err == ESP_OK) {
            err = nvs_commit(nvs);
        }
        nvs_close(nvs);
    }
    return err;
}

bool keryx_net_bridge(char *url, size_t url_size, char *token, size_t token_size)
{
    bool has_url = setting_get("bridge", url, url_size);
    bool has_token = setting_get("token", token, token_size);
    return has_url && has_token;
}

static const char *reason_name(int reason)
{
    switch (reason) {
    case WIFI_REASON_NO_AP_FOUND: return "no_ap_found";
    case WIFI_REASON_AUTH_FAIL: return "auth_fail";
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT: return "wrong_password";
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "handshake_timeout";
    case WIFI_REASON_ASSOC_FAIL: return "assoc_fail";
    case WIFI_REASON_BEACON_TIMEOUT: return "beacon_timeout";
    case WIFI_REASON_CONNECTION_FAIL: return "connection_fail";
    case WIFI_REASON_AUTH_EXPIRE: return "auth_expire";
    default: return "other";
    }
}

// Applies the stored Wi-Fi settings: leaves the current network and joins the configured one, if any.
static void wifi_apply(void)
{
    if (!started) {
        return;
    }
    esp_timer_stop(retry_timer);
    retry_s = RETRY_MIN_S;
    esp_wifi_disconnect();  // its disconnect event has reason ASSOC_LEAVE, which does not schedule a retry

    wifi_config_t cfg = {0};
    char ssid[33], password[65];
    if (!setting_get("ssid", ssid, sizeof(ssid))) {
        return;
    }
    setting_get("password", password, sizeof(password));
    memcpy(cfg.sta.ssid, ssid, strlen(ssid));
    memcpy(cfg.sta.password, password, strlen(password));
    cfg.sta.threshold.authmode = password[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    cfg.sta.pmf_cfg.capable = true;
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
    esp_wifi_connect();
}

static void retry_connect(void *arg)
{
    esp_wifi_connect();
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        wifi_apply();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = data;
        bool was_connected = connected;
        connected = false;
        if (event->reason == WIFI_REASON_ASSOC_LEAVE) {
            if (was_connected) {
                keryx_console_printf("wifi disconnected reason=left\n");
            }
            return;  // we left on purpose: new settings, or erase
        }
        keryx_console_printf("wifi disconnected reason=%s (%d) retry_in=%ds\n", reason_name(event->reason),
                             event->reason, retry_s);
        esp_timer_start_once(retry_timer, retry_s * 1000000LL);
        retry_s = retry_s * 2 > RETRY_MAX_S ? RETRY_MAX_S : retry_s * 2;
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        connected = true;
        retry_s = RETRY_MIN_S;
        wifi_ap_record_t ap;
        int rssi = esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
        keryx_console_printf("wifi connected ip=" IPSTR " rssi=%d\n", IP2STR(&event->ip_info.ip), rssi);
    }
}

static esp_err_t net_start(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }

#define CHECK(x, what)                                                                                   \
    do {                                                                                                 \
        esp_err_t e = (x);                                                                               \
        if (e != ESP_OK) {                                                                               \
            snprintf(start_error, sizeof(start_error), "%s: %s", what, esp_err_to_name(e));              \
            return e;                                                                                    \
        }                                                                                                \
    } while (0)
    // our own `wifi connected/disconnected` lines say what matters; the driver's state log only crowds the console
    esp_log_level_set("wifi", ESP_LOG_WARN);
    esp_log_level_set("wifi_init", ESP_LOG_WARN);
    esp_log_level_set("esp_netif_handlers", ESP_LOG_WARN);
    esp_log_level_set("phy_init", ESP_LOG_WARN);
    CHECK(esp_netif_init(), "netif");
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(netif, keryx_net_id());

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    CHECK(esp_wifi_init(&init), "wifi init");
    CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM), "wifi storage");  // ours is in NVS already
    CHECK(esp_wifi_set_mode(WIFI_MODE_STA), "wifi mode");

    const esp_timer_create_args_t timer = {.callback = retry_connect, .name = "wifi_retry"};
    CHECK(esp_timer_create(&timer, &retry_timer), "timer");
    CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL), "wifi events");
    CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL), "ip events");
    started = true;
    CHECK(esp_wifi_start(), "wifi start");  // STA_START then joins the stored network, if any
    return ESP_OK;
#undef CHECK
}

esp_err_t keryx_net_start(void)
{
    esp_err_t err = net_start();
    if (err != ESP_OK && start_error[0] == '\0') {
        snprintf(start_error, sizeof(start_error), "%s", esp_err_to_name(err));
    }
    return err;
}

static bool is_secret(const char *key)
{
    for (size_t i = 0; i < sizeof(SECRET_KEYS) / sizeof(SECRET_KEYS[0]); i++) {
        if (strcmp(key, SECRET_KEYS[i]) == 0) {
            return true;
        }
    }
    return false;
}

static void command_set(const char *args)
{
    // "<key> <value>": the value is the rest of the line; "set password" alone clears it (open network)
    const char *space = strchr(args, ' ');
    size_t key_len = space ? (size_t)(space - args) : strlen(args);
    const char *value = space ? space + 1 : "";
    const char *key = NULL;
    for (size_t i = 0; i < sizeof(KEYS) / sizeof(KEYS[0]); i++) {
        if (strlen(KEYS[i]) == key_len && strncmp(args, KEYS[i], key_len) == 0) {
            key = KEYS[i];
        }
    }
    size_t len = strlen(value);
    if (key == NULL) {
        keryx_console_printf("error unknown setting (ssid, password, bridge, token)\n");
    } else if (strcmp(key, "ssid") == 0 && (len == 0 || len > 32)) {
        keryx_console_printf("error ssid must be 1-32 bytes\n");
    } else if (strcmp(key, "password") == 0 && len != 0 && (len < 8 || len > 63)) {
        keryx_console_printf("error password must be 8-63 characters, or empty for an open network\n");
    } else if (strcmp(key, "bridge") == 0 &&
               (len > URL_MAX || (strncmp(value, "ws://", 5) != 0 && strncmp(value, "wss://", 6) != 0))) {
        keryx_console_printf("error bridge must be a ws:// or wss:// URL of up to %d characters\n", URL_MAX);
    } else if (strcmp(key, "token") == 0 && (len == 0 || len > TOKEN_MAX)) {
        keryx_console_printf("error token must be 1-%d characters\n", TOKEN_MAX);
    } else {
        esp_err_t err = setting_set(key, value);
        if (err != ESP_OK) {
            keryx_console_printf("error saving %s: %s\n", key, esp_err_to_name(err));
            return;
        }
        keryx_console_printf("ok %s=%s\n", key, is_secret(key) ? "(set)" : value);
        if (strcmp(key, "password") == 0) {
            wifi_apply();  // not on `set ssid`: pairing sets the password next, and joining before it only fails
        }
    }
}

static void command_config(void)
{
    for (size_t i = 0; i < sizeof(KEYS) / sizeof(KEYS[0]); i++) {
        char value[URL_MAX + 1];
        bool set = setting_get(KEYS[i], value, sizeof(value));
        keryx_console_printf("%s=%s\n", KEYS[i], !set ? "(not set)" : is_secret(KEYS[i]) ? "(set)" : value);
    }
    keryx_console_printf("ok\n");
}

static void command_erase(void)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err == ESP_OK) {
        err = nvs_erase_all(nvs);
        if (err == ESP_OK) {
            err = nvs_commit(nvs);
        }
        nvs_close(nvs);
    }
    if (err != ESP_OK) {
        keryx_console_printf("error erasing: %s\n", esp_err_to_name(err));
        return;
    }
    wifi_apply();
    keryx_console_printf("ok settings erased\n");
}

static const char *auth_name(wifi_auth_mode_t mode)
{
    switch (mode) {
    case WIFI_AUTH_OPEN: return "open";
    case WIFI_AUTH_WEP: return "wep";
    case WIFI_AUTH_WPA_PSK: return "wpa";
    case WIFI_AUTH_WPA2_PSK: return "wpa2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "wpa/wpa2";
    case WIFI_AUTH_WPA3_PSK: return "wpa3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "wpa2/wpa3";
    default: return "other";
    }
}

static void command_scan(void)
{
    esp_err_t err = esp_wifi_scan_start(NULL, true);
    if (err != ESP_OK) {
        keryx_console_printf("error scan: %s (busy connecting? try again in a few seconds)\n", esp_err_to_name(err));
        return;
    }
    uint16_t count = SCAN_MAX;
    wifi_ap_record_t *aps = calloc(SCAN_MAX, sizeof(wifi_ap_record_t));
    if (aps == NULL) {
        esp_wifi_clear_ap_list();
        keryx_console_printf("error out of memory\n");
        return;
    }
    esp_wifi_scan_get_ap_records(&count, aps);
    for (int i = 0; i < count; i++) {
        keryx_console_printf("rssi=%d ch=%d auth=%s ssid=%s\n", aps[i].rssi, aps[i].primary, auth_name(aps[i].authmode),
                             (const char *)aps[i].ssid);
    }
    free(aps);
    keryx_console_printf("ok %u networks\n", count);
}

static void command_status(void)
{
    keryx_console_printf("id=%s firmware=%s internal_free=%u internal_min=%u internal_largest=%u psram_free=%u\n",
                         keryx_net_id(), esp_app_get_description()->version,
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                         (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    char ssid[33];
    if (start_error[0] != '\0') {
        keryx_console_printf("wifi=failed_to_start (%s)\n", start_error);
    } else if (!setting_get("ssid", ssid, sizeof(ssid))) {
        keryx_console_printf("wifi=not_configured\n");
    } else if (connected) {
        esp_netif_ip_info_t ip;
        wifi_ap_record_t ap;
        esp_netif_get_ip_info(netif, &ip);
        bool ap_ok = esp_wifi_sta_get_ap_info(&ap) == ESP_OK;
        keryx_console_printf("wifi=connected ssid=%s ip=" IPSTR " rssi=%d ch=%d\n", ssid, IP2STR(&ip.ip),
                             ap_ok ? ap.rssi : 0, ap_ok ? ap.primary : 0);
    } else {
        keryx_console_printf("wifi=connecting ssid=%s\n", ssid);
    }
    char url[URL_MAX + 1], token[TOKEN_MAX + 1];
    bool url_set = setting_get("bridge", url, sizeof(url));
    bool token_set = setting_get("token", token, sizeof(token));
    keryx_console_printf("bridge=%s token=%s\n", url_set ? url : "(not set)", token_set ? "(set)" : "(not set)");
    keryx_console_printf("ok\n");
}

// Opens a TCP connection to host:port and closes it: can the board reach the bridge from this network?
static void command_check(const char *args)
{
    char host[96];
    int port;
    if (sscanf(args, "%95s %d", host, &port) != 2 || port <= 0 || port > 65535) {
        keryx_console_printf("error usage: net check <host> <port>\n");
        return;
    }
    if (!connected) {
        keryx_console_printf("error not on Wi-Fi\n");
        return;
    }
    int64_t start = esp_timer_get_time();
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM}, *res = NULL;
    char port_str[8];
    snprintf(port_str, sizeof(port_str), "%d", port);
    if (getaddrinfo(host, port_str, &hints, &res) != 0 || res == NULL) {
        keryx_console_printf("error cannot resolve %s\n", host);
        return;
    }
    char ip[16];
    inet_ntoa_r(((struct sockaddr_in *)res->ai_addr)->sin_addr, ip, sizeof(ip));
    // non-blocking connect, so that a network that drops the SYN fails in CHECK_TIMEOUT_S, not TCP's ~18 s
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    int rc = -1, err = ENOMEM;
    if (sock >= 0) {
        fcntl(sock, F_SETFL, fcntl(sock, F_GETFL, 0) | O_NONBLOCK);
        rc = connect(sock, res->ai_addr, res->ai_addrlen);
        err = errno;
        if (rc != 0 && err == EINPROGRESS) {
            fd_set writable;
            FD_ZERO(&writable);
            FD_SET(sock, &writable);
            struct timeval timeout = {.tv_sec = CHECK_TIMEOUT_S};
            if (select(sock + 1, NULL, &writable, NULL, &timeout) == 1) {
                socklen_t len = sizeof(err);
                getsockopt(sock, SOL_SOCKET, SO_ERROR, &err, &len);
                rc = err == 0 ? 0 : -1;
            } else {
                err = ETIMEDOUT;
            }
        }
    }
    freeaddrinfo(res);
    if (sock >= 0) {
        close(sock);
    }
    int ms = (int)((esp_timer_get_time() - start) / 1000);
    if (rc == 0) {
        keryx_console_printf("ok %s:%d (%s) reachable in %d ms\n", host, port, ip, ms);
    } else {
        keryx_console_printf("error %s:%d (%s) not reachable: %s after %d ms\n", host, port, ip, strerror(err), ms);
    }
}

bool keryx_net_command(const char *cmd)
{
    if (strncmp(cmd, "net check ", 10) == 0) {
        command_check(cmd + 10);
        return true;
    }
    if (strncmp(cmd, "set ", 4) == 0) {
        command_set(cmd + 4);
    } else if (strcmp(cmd, "config") == 0) {
        command_config();
    } else if (strcmp(cmd, "erase") == 0) {
        command_erase();
    } else if (strcmp(cmd, "wifi scan") == 0) {
        command_scan();
    } else if (strcmp(cmd, "status") == 0) {
        command_status();
    } else {
        return false;
    }
    return true;
}
