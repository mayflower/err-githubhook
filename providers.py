import hashlib
import hmac

from errbot.templating import tenv

GITHUB_EVENTS = ['commit_comment', 'create', 'delete', 'deployment',
                 'deployment_status', 'fork', 'gollum', 'issue_comment',
                 'issues', 'member', 'page_build', 'public',
                 'pull_request_review_comment', 'pull_request', 'push',
                 'release', 'status', 'team_add', 'watch', '*']
GITLAB_EVENTS = ['push_hook', 'tag_push_hook', 'issue_hook', 'note_hook', 'merge_request_hook']
SUPPORTED_EVENTS = GITHUB_EVENTS + GITLAB_EVENTS
DEFAULT_EVENTS = ['commit_comment', 'issue_comment', 'issues', 'pull_request_review_comment',
                  'pull_request', 'push', 'push_hook', 'tag_push_hook', 'issue_hook',
                  'note_hook', 'merge_request_hook']


class CommonGitWebProvider(object):
    def create_message(self, body, event_type, repo):
        """
        Dispatch the message. Check explicitly with hasattr first. When
        using a try/catch with AttributeError errors in the
        message_function which result in an AttributeError would cause
        us to call msg_generic, which is not what we want.
        """
        message_function = 'msg_{0}'.format(event_type)
        if hasattr(self, message_function):
            message = getattr(self, message_function)(body, repo)
        else:
            message = self.msg_generic(body, repo, event_type)
        return message

    def render_template(self, template='generic', **kwargs):
        kwargs['repo_name'] = kwargs.get('repo_name') or self.name
        return tenv().get_template('{0}.html'.format(template)).render(**kwargs)

    def msg_generic(self, body, repo, event_type):
        return self.render_template(
            template='generic', body=body, repo=repo, event_type=event_type)


class GithubHandlers(CommonGitWebProvider):
    name = 'Github'

    @staticmethod
    def valid_message(request, token):
        """Validate the signature of the incoming payload.

        The header received from Github is in the form of algorithm=hash.
        """
        # TODO: Fix GitLab token validation:
        #       https://docs.gitlab.com/ce/web_hooks/web_hooks.html#secret-token
        signature = request.get_header('X-Hub-Signature')

        if signature is None:
            return False

        try:
            alg, sig = signature.split('=')
        except ValueError:
            return False

        if alg != 'sha1':
            return False

        message = request.body.read()
        mac = hmac.new(token.encode(), msg=message, digestmod=hashlib.sha1).hexdigest()
        return hmac.compare_digest(mac, sig)

    def get_repo(self, body):
        return body['repository']['full_name']

    def msg_issues(self, body, repo):
        return self.render_template(
            template='issues', body=body, repo=repo,
            action=body['action'],
            number=body['issue']['number'],
            title=body['issue']['title'],
            user=body['issue']['user']['login'],
            url=body['issue']['url'],
            is_assigned=body['issue']['assignee'],
            assignee=body['issue']['assignee']['login'] if body['issue']['assignee'] else None
        )

    def msg_pull_request(self, body, repo):
        action = body['action']
        user = body['pull_request']['user']['login']
        if action == 'closed' and body['pull_request']['merged']:
            user = body['pull_request']['merged_by']['login']
            action = 'merged'
        if action == 'synchronize':
            action = 'updated'

        return self.render_template(
            template='pull_request', body=body, repo=repo,
            action=action, user=user,
            number=body['pull_request']['number'],
            url=body['pull_request']['html_url'],
            merged=body['pull_request']['merged'],
        )

    def msg_pull_request_review_comment(self, body, repo):
        return self.render_template(
            template='pull_request_review_comment', body=body, repo=repo,
            action='commented' if body['action'] == 'created' else body['action'],
            user=body['comment']['user']['login'],
            line=body['comment']['position'],
            l_url=body['comment']['html_url'],
            pr=body['pull_request']['number'],
            url=body['pull_request']['html_url'],
        )

    def msg_push(self, body, repo):
        return self.render_template(
            template='push', body=body, repo=repo,
            user=body['pusher']['name'],
            commits=len(body['commits']),
            branch=body['ref'].split('/')[-1],
            url=body['compare'],
        )

    def msg_status(*args):
        """Status events are crazy and free form. There's no sane, consistent
        or logical way to deal with them."""
        return None

    def msg_issue_comment(self, body, repo):
        return self.render_template(
            template='issue_comment', body=body, repo=repo,
            action='commented' if body['action'] == 'created' else body['action'],
            user=body['comment']['user']['login'],
            number=body['issue']['number'],
            title=body['issue']['title'],
            url=body['issue']['html_url'],
        )

    def msg_commit_comment(self, body, repo):
        return self.render_template(
            template='commit_comment', body=body, repo=repo,
            user=body['comment']['user']['login'],
            url=body['comment']['html_url'],
            line=body['comment']['line'],
            sha=body['comment']['commit_id'],
        )


class GitLabHandlers(CommonGitWebProvider):
    name = 'GitLab'

    @staticmethod
    def valid_message(request, token):
        """Validate the signature of the incoming payload.

        The header received from GitLab is in the form of algorithm=hash.
        # TODO: Fix GitLab token validation:
        #       https://docs.gitlab.com/ce/web_hooks/web_hooks.html#secret-token
        """
        signature = request.get_header('X-Gitlab-Token')
        return True

    def get_repo(self, body):
        return body['project']['name']

    def map_event_type(self, event_type):
        return {
            'push_hook': 'push',
            'issue_hook': 'issue',
            'note_hook': 'comment',
        }.get(event_type)

    def create_message(self, body, event_type, repo):
        mapped_event_type = self.map_event_type(event_type)
        return super(GitLabHandlers, self).create_message(body, mapped_event_type, repo)

    def msg_push(self, body, repo):
        if body['commits']:
            last_commit_url = body['commits'][-1]['url']
            commit_messages = [
                dict(msg=c['message'][:80].split('\n')[0], hash=c['id'][:8],
                     url=c['url']) for c in body['commits']
            ]
        else:
            last_commit_url = body['project']['web_url']
            commit_messages = []

        return self.render_template(
            template='push', body=body, repo=repo,
            user=body['user_name'],
            commits=len(body['commits']),
            branch='/'.join(body['ref'].split('/')[2:]),
            url=last_commit_url,
            commit_messages=commit_messages,
        )

    def msg_issue(self, body, repo):
        action = {'reopen': 'reopened', 'close': 'closed', 'open': 'opened'}.get(body['object_attributes']['action'])
        if action:
            return self.render_template(
                template='issues', body=body, repo=repo,
                action=action,
                title=body['object_attributes']['title'],
                user=body['user']['name'],
                url=body['object_attributes']['url']
            )

    def msg_comment(self, body, repo):
        noteable = body['object_attributes']['noteable_type'].lower()
        if noteable == "issue":
            return self.render_template(
                template='issue_comment', body=body, repo=repo,
                user=body['user']['name'],
                url=body['object_attributes']['url'],
                action='commented',
                title=body['issue']['title']
            )
        elif noteable == "commit":
            return self.render_template(
                template='commit_comment', body=body, repo=repo,
                user=body['user']['name'],
                url=body['object_attributes']['url'],
                line=None,
            )
        elif noteable == "mergerequest":
            return self.render_template(
                template='merge_request_comment', body=body, repo=repo,
                user=body['user']['name'],
                url=body['object_attributes']['url'],
            )

