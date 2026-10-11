import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CircleAlert,
  DatabaseZap,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
} from "lucide-react";

import { CopyForLlm } from "../../components/CopyForLlm";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui";
import { numberLocale, type AppLocale } from "../../lib/i18n";
import {
  allowedCrawlerActions,
  calculateCrawlerMetrics,
  crawlerErrorClipboardContext,
  CRAWLER_HEARTBEAT_STALE_SECONDS,
  createIdempotencyKey,
  crawlerHeartbeatFreshness,
  formatDuration,
  safeCrawlerErrorPayload,
} from "./model";
import type {
  CrawlerAction,
  CrawlerApi,
  CrawlerError,
  CrawlerStatus,
  SafeCrawlerErrorPayload,
  SiteRole,
} from "./types";

type Props = {
  role: SiteRole;
  api: CrawlerApi;
  locale?: AppLocale;
  pollIntervalMs?: number;
  onCopyError?: (payload: SafeCrawlerErrorPayload) => void;
};

const phaseLabel = (phase: CrawlerStatus["phase"], locale: AppLocale) =>
  ({
    ru: {
      idle: "Не подключён",
      session: "Сессия",
      catalog: "Каталог",
      queue: "Очередь",
      scan: "Сканирование",
      items: "Предметы",
      persist: "Сохранение",
      publish: "Публикация БД",
      complete: "Завершено",
    },
    en: {
      idle: "Not connected",
      session: "Session",
      catalog: "Catalog",
      queue: "Queue",
      scan: "Scanning",
      items: "Items",
      persist: "Saving",
      publish: "Publishing DB",
      complete: "Complete",
    },
  })[locale][phase];

const statusTone = (status: CrawlerStatus["status"]) => {
  if (status === "completed" || status === "running")
    return "positive" as const;
  if (status === "failed") return "negative" as const;
  if (status === "pausing" || status === "retrying" || status === "starting")
    return "warning" as const;
  return "neutral" as const;
};

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="rounded-md border border-border bg-background/40 p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
      {detail && (
        <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
      )}
    </div>
  );
}

function ErrorRow({
  status,
  error,
  locale,
  onCopyError,
}: {
  status: CrawlerStatus;
  error: CrawlerError;
  locale: AppLocale;
  onCopyError?: Props["onCopyError"];
}) {
  const safe = safeCrawlerErrorPayload(status, error);
  const context = crawlerErrorClipboardContext(safe);
  return (
    <Alert
      title={`${error.errorClass}: ${error.code}`}
      action={
        onCopyError ? (
          <Button variant="outline" onClick={() => onCopyError(safe)}>
            {locale === "ru" ? "В чат" : "Copy for LLM"}
          </Button>
        ) : (
          <CopyForLlm message={error.message} context={context} locale={locale} />
        )
      }
    >
      {error.message} ·{" "}
      {error.retryable
        ? locale === "ru"
          ? "можно повторить"
          : "retryable"
        : locale === "ru"
          ? "нужна проверка"
          : "review required"}
    </Alert>
  );
}

export function CrawlerConsole({
  role,
  api,
  locale = "ru",
  pollIntervalMs = 5000,
  onCopyError,
}: Props) {
  const [status, setStatus] = useState<CrawlerStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [operation, setOperation] = useState<CrawlerAction | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        setStatus(await api.getStatus(signal));
        setApiError(null);
      } catch (error) {
        if (!signal?.aborted)
          setApiError(
            error instanceof Error
              ? error.message
              : "crawler_status_unavailable",
          );
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [api],
  );

  useEffect(() => {
    if (role !== "admin") return;
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(
      () => void refresh(controller.signal),
      Math.max(2000, pollIntervalMs),
    );
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [pollIntervalMs, refresh, role]);

  const execute = async (action: CrawlerAction) => {
    if (!status || operation) return;
    setOperation(action);
    try {
      const next = await api.execute(action, {
        expectedRevision: status.revision,
        idempotencyKey: createIdempotencyKey(
          action,
          status.runId,
          status.revision,
        ),
      });
      setStatus(next);
      setApiError(null);
    } catch (error) {
      setApiError(
        error instanceof Error ? error.message : `crawler_${action}_failed`,
      );
      void refresh();
    } finally {
      setOperation(null);
    }
  };

  const metrics = useMemo(
    () => (status ? calculateCrawlerMetrics(status) : null),
    [status],
  );
  const heartbeat = crawlerHeartbeatFreshness(status?.updatedAt ?? null);
  const activeStatus =
    status?.status === "starting" ||
    status?.status === "running" ||
    status?.status === "pausing" ||
    status?.status === "retrying";
  const actions = status ? allowedCrawlerActions(status) : [];
  const tr = (ru: string, en: string) => (locale === "ru" ? ru : en);
  const localeCode = numberLocale(locale);

  if (role !== "admin") {
    return (
      <Alert
        title={tr(
          "Раздел доступен только администратору",
          "This section is restricted to administrators",
        )}
      >
        {tr(
          "Состояние краулера, ошибки и операции управления не выдаются ролям guest, registered и approved.",
          "Crawler state, errors, and controls are not exposed to guest, registered, or approved roles.",
        )}
      </Alert>
    );
  }

  return (
    <section className="space-y-4" aria-label="Crawler operations">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold">
            <DatabaseZap className="size-5 text-emerald-400" />
            {tr("Краулер рынка", "Market crawler")}
          </h2>
          <p className="text-sm text-muted-foreground">
            {tr(
              "Скорость, покрытие, очередь, checkpoint и безопасное управление запуском.",
              "Speed, coverage, queue, checkpoint, and safe run controls.",
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {status && (
            <Badge tone={statusTone(status.status)}>
              {status.status} · {phaseLabel(status.phase, locale)}
            </Badge>
          )}
          <Button
            variant="outline"
            disabled={loading}
            onClick={() => void refresh()}
          >
            <RefreshCw className="mr-2 size-4" />
            {tr("Обновить", "Refresh")}
          </Button>
        </div>
      </div>

      {apiError && (
        <Alert
          title={tr("Crawler API недоступен", "Crawler API is unavailable")}
          action={
            <CopyForLlm
              locale={locale}
              message={apiError}
              context={{
                severity: "error",
                state: status?.status ?? "unknown",
                revision: status?.revision ?? null,
                endpoint: "/admin/crawler/status",
              }}
            />
          }
        >
          {tr(
            "Последнее подтверждённое состояние сохранено на экране; операция не считается выполненной.",
            "The last confirmed state remains visible; the operation is not treated as completed.",
          )}
        </Alert>
      )}

      {!status && !apiError && (
        <Card>
          <CardContent className="pt-5 text-sm text-muted-foreground">
            {tr("Загружаю состояние…", "Loading status…")}
          </CardContent>
        </Card>
      )}

      {status && !status.configured && (
        <Alert
          title={tr("Краулер ещё не подключён", "Crawler is not connected yet")}
          action={
            <CopyForLlm
              locale={locale}
              message="Crawler control plane is reachable, but no worker has connected yet; counters and timestamps are unknown rather than zero measurements."
              context={{
                severity: "warning",
                state: "not_connected",
                revision: status.revision,
                endpoint: "/admin/crawler/status",
              }}
            />
          }
        >
          {tr(
            "Метрики, скорость и время обновления появятся после первого подтверждённого worker connect. Нулевые значения не считаются измерениями.",
            "Metrics, speed, and update time appear after the first confirmed worker connection. Zero values are not treated as measurements.",
          )}
        </Alert>
      )}

      {status && status.configured && activeStatus && heartbeat.state === "stale" && (
        <Alert
          title={tr(
            "Heartbeat краулера устарел",
            "Crawler heartbeat is stale",
          )}
          action={
            <CopyForLlm
              locale={locale}
              message="Crawler is marked active, but its last server-confirmed heartbeat is older than the UI freshness threshold."
              context={{
                severity: "warning",
                state: status.status,
                runId: status.runId,
                revision: status.revision,
                phase: status.phase,
                updatedAt: status.updatedAt,
                ageSeconds: heartbeat.ageSeconds,
                thresholdSeconds: CRAWLER_HEARTBEAT_STALE_SECONDS,
              }}
            />
          }
        >
          {tr(
            `Последнее подтверждение было ${formatDuration(heartbeat.ageSeconds, "ru")} назад. Скорость и ETA ниже относятся к последнему сохранённому состоянию, а не к живому worker.`,
            `The last confirmation was ${formatDuration(heartbeat.ageSeconds, "en")} ago. Speed and ETA below describe the last saved state, not a live worker.`,
          )}
        </Alert>
      )}

      {status && status.configured && heartbeat.state === "future" && (
        <Alert
          title={tr(
            "Время crawler status находится в будущем",
            "Crawler status timestamp is in the future",
          )}
          action={
            <CopyForLlm
              locale={locale}
              message="Crawler status carries a future server timestamp; freshness and rate metrics are not trustworthy until the clock/status source is corrected."
              context={{
                severity: "error",
                state: "future_timestamp",
                source: status.source ?? "control_plane",
                runId: status.runId,
                revision: status.revision,
                economyRevision: status.economyRevision ?? null,
                updatedAt: status.updatedAt,
                ageSeconds: heartbeat.ageSeconds,
              }}
            />
          }
        >
          {tr(
            "Серверное время статуса больше текущего времени. Метрики скорости и свежести нельзя считать подтверждёнными.",
            "The status server time is later than the current time. Speed and freshness metrics are not confirmed.",
          )}
        </Alert>
      )}

      {status && status.configured && metrics && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <Metric
              label={tr("Покрытие", "Coverage")}
              value={`${metrics.coveragePercent.toFixed(1)}%`}
              detail={`${status.processedItems.toLocaleString(localeCode)} / ${status.totalItems.toLocaleString(localeCode)} ${status.workUnit === "server_scopes" ? tr("рыночных областей", "market scopes") : tr("предметов", "items")}`}
            />
            <Metric
              label={tr("Скорость", "Speed")}
              value={`${metrics.itemsPerSecond.toFixed(2)} ${status.workUnit === "server_scopes" ? "scope/s" : "item/s"}`}
              detail={`${metrics.requestsPerSecond.toFixed(2)} req/s`}
            />
            <Metric
              label="ETA"
              value={formatDuration(metrics.etaSeconds, locale)}
              detail={`${tr("Осталось", "Remaining")} ${metrics.remainingItems.toLocaleString(localeCode)} ${status.workUnit === "server_scopes" ? tr("рыночных областей", "market scopes") : tr("предметов", "items")}`}
            />
            <Metric
              label={tr("Очередь", "Queue")}
              value={status.queueDepth.toLocaleString(localeCode)}
              detail={`${status.retriedRequests} ${tr("повторов", "retries")} · ${status.failedRequests} ${tr("ошибок", "errors")}`}
            />
            <Metric
              label="Heartbeat"
              value={
                heartbeat.state === "fresh"
                  ? tr("свежий", "fresh")
                  : heartbeat.state === "stale"
                    ? tr("устарел", "stale")
                    : heartbeat.state === "future"
                      ? tr("в будущем", "future")
                    : tr("неизвестно", "unknown")
              }
              detail={
                heartbeat.ageSeconds === null
                  ? tr("нет подтверждённого времени", "no confirmed time")
                  : heartbeat.state === "future"
                    ? `${formatDuration(Math.abs(heartbeat.ageSeconds), locale)} ${tr("в будущем", "in the future")}`
                    : `${formatDuration(heartbeat.ageSeconds, locale)} ${tr("назад", "ago")}`
              }
            />
          </div>

          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <div>
                <CardTitle>{tr("Текущий запуск", "Current run")}</CardTitle>
                <div className="mt-1 text-xs text-muted-foreground">
                  {status.source === "standalone_object"
                    ? `run ${status.runId ?? "—"} · economy revision ${status.economyRevision ?? "—"}`
                    : `run ${status.runId ?? "—"} · control revision ${status.revision}`}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                {actions.includes("pause") && (
                  <Button
                    variant="outline"
                    disabled={operation !== null}
                    onClick={() => void execute("pause")}
                  >
                    <Pause className="mr-2 size-4" />
                    {tr("Пауза", "Pause")}
                  </Button>
                )}
                {actions.includes("resume") && (
                  <Button
                    disabled={operation !== null}
                    onClick={() => void execute("resume")}
                  >
                    <Play className="mr-2 size-4" />
                    {tr("Продолжить", "Resume")}
                  </Button>
                )}
                {actions.includes("retry") && (
                  <Button
                    variant="outline"
                    disabled={operation !== null}
                    onClick={() => void execute("retry")}
                  >
                    <RotateCcw className="mr-2 size-4" />
                    {tr("Повторить", "Retry")}
                  </Button>
                )}
              </div>
            </CardHeader>
            <CardContent>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-emerald-500 transition-[width]"
                  style={{ width: `${metrics.coveragePercent}%` }}
                />
              </div>
              <div className="mt-4 grid gap-3 text-sm md:grid-cols-3">
                <div>
                  <span className="text-muted-foreground">
                    {tr("Успешных запросов:", "Successful requests:")}
                  </span>{" "}
                  {status.successfulRequests.toLocaleString(localeCode)}
                </div>
                <div>
                  <span className="text-muted-foreground">
                    {tr("Прошло:", "Elapsed:")}
                  </span>{" "}
                  {formatDuration(status.elapsedSeconds, locale)}
                </div>
                <div>
                  <span className="text-muted-foreground">
                    {tr("Обновлено:", "Updated:")}
                  </span>{" "}
                  {status.updatedAt
                    ? new Date(status.updatedAt).toLocaleString(localeCode)
                    : "—"}
                </div>
                {status.nextRunAt && (
                  <div>
                    <span className="text-muted-foreground">
                      {tr("Следующий запуск:", "Next run:")}
                    </span>{" "}
                    {new Date(status.nextRunAt).toLocaleString(localeCode)}
                  </div>
                )}
              </div>
              <div className="mt-3 rounded-md border border-border p-3 text-sm">
                <div className="font-medium">
                  {tr("Последняя успешная публикация", "Last successful publication")}
                </div>
                {status.lastSuccess ? (
                  <div className="mt-1 text-muted-foreground">
                    run {status.lastSuccess.runId ?? "—"} · {status.lastSuccess.economyRevision} ·{" "}
                    {new Date(status.lastSuccess.observedAt).toLocaleString(localeCode)} ·{" "}
                    {status.lastSuccess.marketScopeCount.toLocaleString(localeCode)} / 100 {tr("рыночных областей", "market scopes")}
                    {" · "}{(status.lastSuccess.transportAttemptCount ?? status.lastSuccess.requestCount).toLocaleString(localeCode)} {tr("сетевых попыток", "transport attempts")}
                  </div>
                ) : (
                  <div className="mt-1 text-muted-foreground">
                    {tr("ещё не подтверждена", "not confirmed yet")}
                  </div>
                )}
              </div>
              <div className="mt-3 rounded-md border border-border p-3 text-sm">
                <div className="font-medium">
                  {tr("Контрольная точка", "Checkpoint")}
                </div>
                {status.checkpoint ? (
                  <div className="mt-1 text-muted-foreground">
                    {tr("ревизия", "revision")} {status.checkpoint.revision} ·{" "}
                    {status.checkpoint.processedItems.toLocaleString(
                      localeCode,
                    )}{" "}
                    {tr("предметов", "items")} · {tr("курсор", "cursor")}{" "}
                    {status.checkpoint.cursor ?? "—"} ·{" "}
                    {new Date(status.checkpoint.savedAt).toLocaleString(
                      localeCode,
                    )}
                  </div>
                ) : (
                  <div className="mt-1 text-muted-foreground">
                    {tr("Ещё не сохранён", "Not saved yet")}
                  </div>
                )}
              </div>
            </CardContent>
          </Card>

          {status.errors.length > 0 && (
            <div className="space-y-2">
              <h3 className="flex items-center gap-2 font-medium">
                <CircleAlert className="size-4 text-amber-300" />
                {tr("Последние ошибки", "Recent errors")}
              </h3>
              {status.errors.slice(0, 20).map((error) => (
                <ErrorRow
                  key={error.errorId}
                  status={status}
                  error={error}
                  locale={locale}
                  onCopyError={onCopyError}
                />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
