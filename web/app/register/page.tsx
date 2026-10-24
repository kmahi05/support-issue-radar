import { registerAction } from "@/app/actions";
import { AuthForm } from "@/components/AuthForm";

export default function RegisterPage() {
  return (
    <AuthForm
      title="Create your account"
      submit="Create account"
      action={registerAction}
      fields={[
        { name: "display_name", label: "Name", type: "text", autoComplete: "name" },
        { name: "email", label: "Email", type: "email", autoComplete: "email" },
        {
          name: "password",
          label: "Password (10+ characters)",
          type: "password",
          autoComplete: "new-password",
          minLength: 10,
        },
      ]}
      footer={{ text: "Already registered?", href: "/login", link: "Log in" }}
    />
  );
}
