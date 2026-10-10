import { MerchantDemo } from "@/components/MerchantDemo";

export default function HomePage() {
  return (
    <>
      <h1>Create a payment session</h1>
      <p className="lead">
        As the merchant, create a session and share its hosted checkout link. Payments in this
        demo are simulated.
      </p>
      <MerchantDemo />
    </>
  );
}
