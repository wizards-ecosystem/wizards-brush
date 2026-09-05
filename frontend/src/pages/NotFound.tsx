import { Link } from "react-router-dom";
import { Icon } from "../components/icons";
import { EmptyState } from "../components/ui";

export function NotFound() {
  return (
    <div className="page-pad flex min-h-full items-center justify-center">
      <EmptyState
        icon="studio"
        title="This bench does not exist"
        description="The address points outside the current Wizard's Brush workspace."
        action={
          <Link className="btn" to="/">
            Return to Studio <Icon name="arrow-right" size={15} />
          </Link>
        }
      />
    </div>
  );
}
