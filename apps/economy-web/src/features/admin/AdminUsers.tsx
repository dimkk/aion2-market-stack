import { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";

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
import { economyApi } from "../../lib/api";
import type { AppLocale } from "../../lib/i18n";
import type { AdminUser, SessionState, UserRole } from "../../types";

const roles: Array<Extract<UserRole, "registered" | "approved">> = [
  "registered",
  "approved",
];

export function AdminUsers({
  session,
  locale,
}: {
  session: SessionState;
  locale: AppLocale;
}) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const tr = (ru: string, en: string) => (locale === "ru" ? ru : en);
  const load = () =>
    economyApi
      .users()
      .then((value) => setUsers(value.users))
      .catch((reason: unknown) =>
        setError(
          reason instanceof Error ? reason.message : "admin_users_failed",
        ),
      );
  useEffect(() => {
    void load();
  }, []);
  const setRole = async (
    user: AdminUser,
    role: Extract<UserRole, "registered" | "approved">,
  ) => {
    setBusy(user.id);
    setError(null);
    try {
      const result = await economyApi.setRole(
        user.username,
        role,
        session.csrfToken,
      );
      setUsers((current) =>
        current.map((entry) => (entry.id === user.id ? result.user : entry)),
      );
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "admin_role_update_failed",
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      {error && (
        <Alert
          title={tr(
            "Не удалось обновить пользователей",
            "Could not update users",
          )}
          action={
            <CopyForLlm
              message={`AION2 Economy admin: ${error}`}
              context={{ severity: "error", state: "admin-users-failed" }}
            />
          }
        >
          {error}
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-emerald-300" />
            {tr("Доступ пользователей", "User access")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-auto">
            <table className="w-full min-w-140 text-sm">
              <thead>
                <tr className="text-xs uppercase text-muted-foreground">
                  <th className="p-3 text-left">
                    {tr("Пользователь", "User")}
                  </th>
                  <th className="p-3 text-left">{tr("Роль", "Role")}</th>
                  <th className="p-3 text-right">
                    {tr("Действия", "Actions")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr className="border-t border-border" key={user.id}>
                    <td className="p-3">{user.username}</td>
                    <td className="p-3">
                      <Badge
                        tone={
                          user.role === "admin"
                            ? "positive"
                            : user.role === "approved"
                              ? "neutral"
                              : "warning"
                        }
                      >
                        {user.role}
                      </Badge>
                    </td>
                    <td className="p-3">
                      <div className="flex justify-end gap-2">
                        {roles
                          .filter((role) => role !== user.role)
                          .map((role) => (
                            <Button
                              key={role}
                              variant="outline"
                              disabled={
                                busy === user.id ||
                                user.username === session.username
                              }
                              onClick={() => void setRole(user, role)}
                            >
                              {role}
                            </Button>
                          ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
