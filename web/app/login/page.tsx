import { loginAction } from "@/app/actions";
import { AuthForm } from "@/components/AuthForm";

export default function LoginPage() {
  return (
    <AuthForm
      title="Log in to Support Issue Radar"
      submit="Log in"
      action={loginAction}
      fields={[
        { name: "email", label: "Email", type: "email", autoComplete: "email" },
        { name: "password", label: "Password", type: "password", autoComplete: "current-password" },
      ]}
      footer={{ text: "No account yet?", href: "/register", link: "Create one" }}
    />
  );
}
