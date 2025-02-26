import * as React from 'react';

import OrganizationAvatar from 'sentry/components/avatar/organizationAvatar';
import ProjectAvatar from 'sentry/components/avatar/projectAvatar';
import TeamAvatar from 'sentry/components/avatar/teamAvatar';
import UserAvatar from 'sentry/components/avatar/userAvatar';
import {AvatarProject, OrganizationSummary, Team} from 'sentry/types';

type Props = {
  team?: Team;
  organization?: OrganizationSummary;
  project?: AvatarProject;
} & UserAvatar['props'];

const Avatar = React.forwardRef(function Avatar(
  {hasTooltip = false, user, team, project, organization, ...props}: Props,
  ref: React.Ref<HTMLSpanElement>
) {
  const commonProps = {hasTooltip, forwardedRef: ref, ...props};

  if (user) {
    return <UserAvatar user={user} {...commonProps} />;
  }

  if (team) {
    return <TeamAvatar team={team} {...commonProps} />;
  }

  if (project) {
    return <ProjectAvatar project={project} {...commonProps} />;
  }

  return <OrganizationAvatar organization={organization} {...commonProps} />;
});

export default Avatar;
